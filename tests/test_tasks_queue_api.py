"""One task in progress per folder, over HTTP (server/routers/tasks.py).

The router's half of the project queue: the `queued` status and the four fields
that say where a row stands, the three verbs the client calls (`admit`, `skip`,
`decide`), run-now's queued answer next door, and the two flag-agnostic wins
that came with them (a scoped `gone` on the changes long-poll, and a notify ring
on the read endpoint).

BOTH FLAG STATES, everywhere it can differ. The whole feature is behind
`project_queue_enabled` (prefs, default off) and the promise is that with the
flag off every path behaves exactly as it did before it existed: `queued` never
appears, admission always says run, and nothing is stored.

Who HOLDS a folder is `project_queue.holders()` — a derivation over live
processes with its own suite (tests/test_project_queue.py). Here it is stood in
for, because what is under test is what the router does with the answer. The one
exception is the reservation: `admit` takes it, so the second admit in
`test_a_second_session_queues_behind_the_first_ones_reservation` reads the real
derivation through the real store.

Nothing reads the real ~/.claude or the developer's prefs — every path is under
tmp_path.
"""
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from fused_render import project_queue, schedule, tasks_store, tasks_watch
from fused_render._view_url_codec import canonical_fs_path
from fused_render.server import create_app
from fused_render.server.routers import claude_sessions as sessions_mod
from fused_render.server.routers import tasks as tasks_mod

HEADERS = {"X-Fused": "1"}


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
    """Stand in for the live derivation: `{folder: holder task key}`."""
    built = {key: {"session_id": task_key, "run_id": "r-" + task_key,
                   "task_key": task_key, "kind": "run"}
             for key, task_key in mapping.items()}
    monkeypatch.setattr(project_queue, "holders", lambda now=None: dict(built))
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


def _rows(client):
    r = client.get("/api/tasks")
    assert r.status_code == 200, r.text
    return {task["key"]: task for task in r.json()["tasks"]}


def _post(client, path, body):
    return client.post(path, json=body, headers=HEADERS)


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
        client, projects_dir, folders, monkeypatch, flag):
    """The whole feature in one row. The message is DUE — it would be running
    this second — and the only thing between it and a process is another task
    holding the same working tree."""
    flag()
    alpha = _waiting_pair(projects_dir, folders, monkeypatch)

    rows = _rows(client)
    row = rows["sess-wait"]
    assert row["status"] == "queued"
    assert row["queue_key"] == alpha
    assert row["queue_position"] == 1
    assert row["queue_ahead"] == rows["sess-holder"]["task_id"]
    assert row["queue_ahead_title"] == "holding the folder"
    assert row["queue_priority"] is False
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
    assert row["queue_priority"] is False


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
        client, projects_dir, folders, monkeypatch, flag, park):
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

    project_queue.hold_answer(alpha, "sess-a", "r-a", "req-1",
                              {"raw": {"decision": "allow"}})

    rows = _rows(client)
    row = rows["sess-a"]
    assert row["status"] == "queued"
    assert row["queue_position"] == 1
    assert row["queue_priority"] is True
    assert row["queue_ahead"] == rows["sess-holder"]["task_id"]


def test_a_second_unanswered_card_still_needs_attention(
        client, projects_dir, folders, monkeypatch, flag, park):
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
    project_queue.hold_answer(alpha, "sess-a", "r-a", "req-1",
                              {"raw": {"decision": "allow"}})

    assert _rows(client)["sess-a"]["status"] == "needs_attention"


def test_the_line_puts_held_answers_first_then_priority_then_the_older_due(
        client, projects_dir, folders, monkeypatch, flag, state_dir, park):
    """The order the line moves in, in one row of three. A held answer outranks
    every message in its folder (delivering it is what lets the parked run
    finish and free the tree); a skipped message outranks an unskipped one; and
    between equals the older `due` goes first.

    The answering task has a REAL parked run behind it — an unanswered request
    on disk — because that is the only shape a held answer ever comes in, and a
    case with no run at all would pass without the rule that keeps such a row
    out of `needs_attention`."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _transcript(projects_dir, "sess-answer", alpha, "answered a card")
    park("r-answer", "sess-answer", alpha)
    schedule._write([
        _entry("e-old", "asked first", alpha, due=_iso(-300),
               session_id="sess-plain"),
        _entry("e-skip", "skipped to the front", alpha, due=_iso(-60),
               session_id="sess-skipped", priority=True),
    ])
    project_queue.hold_answer(alpha, "sess-answer", "r-answer", "req-1",
                              {"raw": {"decision": "allow"}})
    _holders(monkeypatch, {alpha: "sess-holder"})

    rows = _rows(client)
    assert rows["sess-answer"]["status"] == "queued"
    assert rows["sess-answer"]["queue_position"] == 1
    assert rows["sess-answer"]["queue_priority"] is True
    assert rows["sess-skipped"]["queue_position"] == 2
    assert rows["sess-skipped"]["queue_priority"] is True
    assert rows["sess-plain"]["queue_position"] == 3
    assert rows["sess-plain"]["queue_priority"] is False
    assert {row["queue_ahead"] for row in rows.values() if row["queue_position"]} \
        == {rows["sess-holder"]["task_id"]}


def test_one_task_takes_one_place_however_many_messages_it_has(
        client, projects_dir, folders, monkeypatch, flag):
    """The line the UI prints is a line of TASKS ("#2 in line"), so a task with
    three queued messages is one thing waiting and not three."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    schedule._write([
        _entry("e1", "one", alpha, due=_iso(-300), session_id="sess-a"),
        _entry("e2", "two", alpha, due=_iso(-200), session_id="sess-a"),
        _entry("e3", "three", alpha, due=_iso(-100), session_id="sess-b"),
    ])
    _holders(monkeypatch, {alpha: "sess-holder"})

    rows = _rows(client)
    assert rows["sess-a"]["queue_position"] == 1
    assert rows["sess-b"]["queue_position"] == 2


def test_two_folders_have_two_independent_lines(
        client, projects_dir, folders, monkeypatch, flag):
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

    rows = _rows(client)
    assert rows["sess-a"]["queue_position"] == 1
    assert rows["sess-b"]["queue_position"] == 1
    assert rows["sess-a"]["queue_key"] != rows["sess-b"]["queue_key"]


def test_a_pending_row_queues_under_its_own_key(
        client, projects_dir, folders, monkeypatch, flag):
    """A brand-new task — a message that names no session at all — is still a
    row, keyed `pending:<entry id>` (§5), and still takes a place in the line."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    schedule._write([_entry("e-new", "a brand new task", alpha)])
    _holders(monkeypatch, {alpha: "sess-holder"})

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
        client, projects_dir, folders, monkeypatch, flag):
    """The sidebar prints "n queued" beside "n running" and a queued row has to
    be able to say whether it runs next — without downloading the Tasks page."""
    flag()
    _waiting_pair(projects_dir, folders, monkeypatch)

    pulse = {row["key"]: row for row in client.get("/api/tasks/pulse").json()["tasks"]}
    row = pulse["sess-wait"]
    assert row["status"] == "queued"
    assert row["queue_position"] == 1
    assert row["queue_ahead"].startswith("TASK-")
    assert row["queue_priority"] is False
    # Compact stays compact: the folder and the holder's title are not here.
    assert "queue_key" not in row
    assert "queue_ahead_title" not in row


def test_the_changes_answer_names_a_holder_it_was_not_asked_about(
        client, projects_dir, folders, monkeypatch, flag):
    """A narrowed long-poll answer is about one row, and the task in front of it
    is by definition another. Naming it from the stored table is what keeps the
    chip from reading "behind " with nothing after it."""
    flag()
    _waiting_pair(projects_dir, folders, monkeypatch)
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


def test_admit_into_a_free_folder_runs_and_takes_a_reservation(
        client, folders, flag):
    """Between "run" and the CLI registering a session there is nothing on disk
    saying the folder is taken. The reservation is what closes that window."""
    flag()
    alpha, _beta = folders
    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "sess-a", "message": "go"})
    assert r.json() == {"run": True}
    assert project_queue.reserved(alpha) == "sess-a"
    assert schedule.list_entries() == []


def test_a_second_session_queues_behind_the_first_ones_reservation(
        client, projects_dir, folders, flag):
    """The real derivation, end to end and unmocked: one admission reserves the
    folder, and the next send into it from another task is stored instead of
    spawned."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha, "holding the folder")
    ahead = _rows(client)["sess-a"]["task_id"]

    assert _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "session_id": "sess-a",
                  "message": "go"}).json() == {"run": True}
    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "sess-b", "message": "me too"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"] is False
    assert body["key"] == "sess-b"
    assert body["position"] == 1
    assert body["ahead"] == ahead
    assert body["ahead_title"] == "holding the folder"
    assert body["entry"]["message"] == "me too"
    assert body["entry"]["state"] == schedule.PENDING
    assert _rows(client)["sess-b"]["status"] == "queued"


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
    assert entry["images"] == [shot]
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
    assert _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "session_id": "sess-a", "message": "",
                  "images": ["/tmp/shot.png"]}).json() == {"run": True}

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
                       ("/api/tasks/queue/skip", {"key": "sess-a"}),
                       ("/api/tasks/queue/decide",
                        {"run_id": "r", "request_id": "q", "session_id": "s",
                         "project": alpha, "decision": "allow", "scope": "once"})):
        r = client.post(path, json=body)
        assert r.status_code == 403, path


# ===================================================================== skip


@pytest.fixture()
def priorities(monkeypatch):
    """Record `schedule.set_priority` — package B's write, stubbed here so this
    suite pins the ARGUMENTS the router sends rather than the store's behaviour
    (which has its own suite next door)."""
    calls = []

    def set_priority(entry_ids, value):
        calls.append((list(entry_ids), value))
        return {"updated": list(entry_ids), "refused": []}

    monkeypatch.setattr(schedule, "set_priority", set_priority, raising=False)
    return calls


def test_skip_promotes_the_due_work_of_a_queued_task(
        client, projects_dir, folders, monkeypatch, flag, priorities, rings):
    """Skip is a statement about the ORDER of what is waiting. The answer is
    always position 1 and never "running now" — it does not interrupt the run in
    flight, and nothing in this app takes a folder off a live process."""
    flag()
    alpha, beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    schedule._write([
        _entry("e-due", "the queued one", alpha, session_id="sess-a"),
        _entry("e-later", "next week", alpha, due=_iso(7 * 86400),
               session_id="sess-a"),
        _entry("e-other", "another folder", beta, session_id="sess-a"),
    ])
    _holders(monkeypatch, {alpha: "sess-holder"})

    r = _post(client, "/api/tasks/queue/skip", {"key": "sess-a"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "position": 1}
    # Only the DUE work in the folder it is waiting on: promoting next week's
    # message would be this verb silently rescheduling work nobody asked about.
    assert priorities == [(["e-due"], True)]
    assert {"sess-a"} in rings


def test_skip_refuses_a_task_that_is_not_queued(
        client, projects_dir, folders, monkeypatch, flag, priorities):
    """A client looking at a stale row, and the refusal is what makes it
    refetch. Nothing is written."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha)
    schedule._write([_entry("e1", "go", alpha, session_id="sess-a")])
    _holders(monkeypatch, {})

    r = _post(client, "/api/tasks/queue/skip", {"key": "sess-a"})
    assert r.status_code == 400
    assert r.json()["error"] == "not queued"
    assert priorities == []


def test_skip_refuses_an_unknown_key_and_a_disabled_queue(
        client, folders, flag, priorities):
    flag(False)
    r = _post(client, "/api/tasks/queue/skip", {"key": "sess-a"})
    assert r.status_code == 409
    assert r.json()["error"] == "project queue is off"
    flag()
    assert _post(client, "/api/tasks/queue/skip",
                 {"key": "nobody"}).status_code == 404
    assert _post(client, "/api/tasks/queue/skip", {"key": ""}).status_code == 400
    assert priorities == []


def test_skipping_a_task_that_already_answered_a_card_is_a_no_op(
        client, projects_dir, folders, monkeypatch, flag, priorities):
    """A held answer is at the head of its folder by definition. From the
    outside that is exactly what Skip asked for, so it is answered rather than
    refused — and nothing is written, because there is nothing to improve."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    _transcript(projects_dir, "sess-a", alpha)
    project_queue.hold_answer(alpha, "sess-a", "run-1", "req-1", {"raw": {}})
    _holders(monkeypatch, {alpha: "sess-holder"})

    r = _post(client, "/api/tasks/queue/skip", {"key": "sess-a"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "position": 1}
    assert priorities == []


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
    assert project_queue.held_answers() == []


def test_the_flag_off_answers_a_card_the_way_main_does(
        client, folders, monkeypatch, flag, agent):
    flag(False)
    alpha, _beta = folders
    _holders(monkeypatch, {alpha: "sess-holder"})

    assert _post(client, "/api/tasks/queue/decide",
                 _decide_body(alpha)).json()["held"] is False
    assert len(agent.calls) == 1
    assert project_queue.held_answers() == []


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
    assert project_queue.held_answers() == []


def test_a_card_answered_while_another_task_holds_the_folder_is_held(
        client, projects_dir, folders, monkeypatch, flag, agent, rings):
    """The one door into a busy folder that is not a message. A parked run given
    its answer now would wake up and start editing a tree another task owns.

    What is stored is the ARGUMENTS, not a decision payload: every rule in
    `_decide` (the scope downgrade, the mode switch, the answer validation) reads
    the live run, so they are evaluated at delivery and not here."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _holders(monkeypatch, {alpha: "sess-holder"})
    ahead = _rows(client)["sess-holder"]["task_id"]

    r = _post(client, "/api/tasks/queue/decide", _decide_body(alpha))
    assert r.status_code == 200, r.text
    assert r.json() == {"held": True, "position": 1, "ahead": ahead,
                        "ahead_title": "holding the folder"}
    assert agent.calls == []
    held = project_queue.held_answers()
    assert len(held) == 1
    assert held[0]["queue_key"] == alpha
    assert held[0]["session_id"] == "sess-a"
    assert held[0]["payload"] == {"raw": {
        "run_id": "run-1", "request_id": "req-1", "decision": "allow",
        "scope": "session", "mode": "acceptEdits", "answers": "", "note": "",
        "custom": ""}}
    assert {"sess-a"} in rings


def test_a_second_click_on_a_held_card_does_not_queue_a_second_answer(
        client, folders, monkeypatch, flag, agent):
    """First writer wins on `(run_id, request_id)` — the same latch
    `_write_decision` applies on disk, applied here so a double-click cannot
    queue two answers to one question."""
    flag()
    alpha, _beta = folders
    _holders(monkeypatch, {alpha: "sess-holder"})

    _post(client, "/api/tasks/queue/decide", _decide_body(alpha))
    _post(client, "/api/tasks/queue/decide", _decide_body(alpha, decision="deny"))
    held = project_queue.held_answers()
    assert len(held) == 1
    assert held[0]["payload"]["raw"]["decision"] == "allow"


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
    assert project_queue.held_answers() == []


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
                        "ahead_title": "holding the folder"}


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


def test_skip_really_moves_the_entry_to_the_head_of_the_line(
        client, projects_dir, folders, monkeypatch, flag):
    """End to end: two tasks waiting on one folder, the second one skips, and
    the line the very next listing derives has them the other way round."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    schedule._write([
        _entry("e-first", "asked first", alpha, due=_iso(-300),
               session_id="sess-first"),
        _entry("e-second", "asked second", alpha, due=_iso(-60),
               session_id="sess-second"),
    ])
    _holders(monkeypatch, {alpha: "sess-holder"})
    assert _rows(client)["sess-second"]["queue_position"] == 2

    assert _post(client, "/api/tasks/queue/skip",
                 {"key": "sess-second"}).json() == {"ok": True, "position": 1}

    stored = {e["id"]: e for e in schedule.list_entries()}
    assert stored["e-second"]["priority"] is True
    assert stored["e-first"].get("priority") is not True
    rows = _rows(client)
    assert rows["sess-second"]["queue_position"] == 1
    assert rows["sess-second"]["queue_priority"] is True
    assert rows["sess-first"]["queue_position"] == 2


def test_run_now_into_a_busy_folder_queues_through_the_real_model(
        client, projects_dir, folders, monkeypatch, flag):
    """The drag, all the way down: nothing is claimed, the entry gains
    `priority`, and the row the Board redraws reads `queued` at position 1."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    schedule._write([_entry("e1", "run it", alpha, session_id="sess-a")])
    _holders(monkeypatch, {alpha: "sess-holder"})
    ahead = _rows(client)["sess-holder"]["task_id"]

    r = _post(client, "/api/schedule/run-now", {"entry_id": "e1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["reason"] == "queued"
    assert body["position"] == 1
    assert body["ahead"] == ahead
    assert body["ahead_title"] == "holding the folder"
    assert body["entry"]["state"] == schedule.PENDING
    assert body["entry"]["priority"] is True

    row = _rows(client)["sess-a"]
    assert row["status"] == "queued"
    assert row["queue_position"] == 1
    assert row["queue_priority"] is True


def test_run_now_answers_the_position_the_row_shows(
        client, projects_dir, folders, monkeypatch, flag):
    """ONE NUMBER FOR ONE LINE. The scheduler counts a folder's line in ENTRIES
    and the Tasks page counts it in TASKS — one slot each, however many messages
    a task has queued — and the page's is the number printed on the row, in the
    chip and in "#2 in line". A drag that answered 3 while the card it just
    moved said 2 is one of them wrong."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _transcript(projects_dir, "sess-b", alpha, "two messages, one place")
    schedule._write([
        _entry("e-b1", "first", alpha, due=_iso(-300), session_id="sess-b",
               priority=True),
        _entry("e-b2", "second", alpha, due=_iso(-200), session_id="sess-b",
               priority=True),
        _entry("e-a1", "mine", alpha, due=_iso(-100), session_id="sess-a"),
    ])
    _holders(monkeypatch, {alpha: "sess-holder"})

    body = _post(client, "/api/schedule/run-now", {"entry_id": "e-a1"}).json()
    # Two entries are in front of it; ONE task is.
    assert body["position"] == 2
    assert body["position"] == _rows(client)["sess-a"]["queue_position"]


def test_run_now_on_a_far_future_message_puts_the_row_in_the_queued_lane(
        client, projects_dir, folders, monkeypatch, flag):
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
        client, folders, monkeypatch, flag):
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
    monkeypatch.setattr(project_queue, "holders", lambda now=None: {
        alpha: {"session_id": "", "run_id": "", "task_key": "pending:e-x",
                "kind": "sending"}})
    ahead = _rows(client)["pending:e-x"]["task_id"]
    assert ahead

    body = _post(client, "/api/schedule/run-now", {"entry_id": "e1"}).json()
    assert body["reason"] == "queued"
    assert body["ahead"] == ahead
    assert body["ahead_title"] == "the claimed one"
    assert _rows(client)["sess-a"]["queue_ahead"] == ahead


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
        client, projects_dir, folders, monkeypatch, flag):
    """One slot per TASK, however many messages it has queued — the follower is
    in its leader's slot, not behind it."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _leader_and_follower(alpha)
    _holders(monkeypatch, {alpha: "sess-holder"})

    row = _rows(client)[tasks_store.pending_key("e-lead")]
    assert row["status"] == "queued"
    assert row["queue_position"] == 1


def test_skip_promotes_a_leader_and_its_follower_together(
        client, projects_dir, folders, monkeypatch, flag):
    """They share a task key, so Skip on the row reaches both — a chat that
    jumped the line with only its first message would send the second one last."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    _leader_and_follower(alpha)
    _holders(monkeypatch, {alpha: "sess-holder"})

    r = _post(client, "/api/tasks/queue/skip",
              {"key": tasks_store.pending_key("e-lead")})
    assert r.json() == {"ok": True, "position": 1}

    stored = {e["id"]: e for e in schedule.list_entries()}
    assert stored["e-lead"]["priority"] is True
    assert stored["e-follow"]["priority"] is True


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
    assert project_queue.reserved(alpha) == ""


def test_admit_runs_when_this_chats_own_work_is_not_due_yet(
        client, folders, flag):
    """A message scheduled for next Tuesday is waiting on the clock, not on the
    folder — queueing behind it would park this send until Tuesday."""
    flag()
    alpha, _beta = folders
    schedule._write([_entry("e1", "next week", alpha, due=_iso(86_400),
                            session_id="sess-a")])

    assert _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "session_id": "sess-a",
                  "message": "now"}).json() == {"run": True}


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


def test_a_follower_does_not_pull_its_task_up_the_line(
        client, projects_dir, folders, monkeypatch, flag):
    """A task takes its place at its BEST entry, and a follower's own due can be
    earlier than its leader's — a clock that moved, a back-dated edit. Without
    the leader's key the row would sit at the earlier slot here while
    `schedule._claim_due` sends it later: a line listed in one order and run in
    another, which is worse than no line at all."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    _transcript(projects_dir, "sess-b", alpha, "somebody else")
    schedule._write([
        _entry("e-lead", "first thing", alpha, due=_iso(-100)),
        _entry("e-follow", "second thing", alpha, due=_iso(-300),
               follow_of="e-lead"),
        _entry("e-b", "the other task", alpha, due=_iso(-200),
               session_id="sess-b"),
    ])
    _holders(monkeypatch, {alpha: "sess-holder"})

    rows = _rows(client)
    lead_key = tasks_store.pending_key("e-lead")
    assert rows["sess-b"]["queue_position"] == 1
    assert rows[lead_key]["queue_position"] == 2
    # …which is the order the scheduler will actually send them in.
    assert schedule._claim_due(schedule._now()) == ["e-b", "e-lead", "e-follow"]


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


# ---------------------------------------------------------------- skip by entry


def test_skip_names_the_entry_when_the_key_has_moved(
        client, projects_dir, folders, monkeypatch, flag, priorities):
    """A chip painted while the chat was `pending:<leader>` still holds that key
    a second after the leader's run mints a session and the whole row rekeys —
    so Skip by key 404s on the one gesture the user is watching the line for.
    The entry id never moves."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha)
    schedule._write([
        _entry("e-lead", "first thing", alpha, state=schedule.SENT,
               turn="done", claude_session_id="sess-lead"),
        _entry("e-follow", "second thing", alpha, follow_of="e-lead"),
    ])
    _holders(monkeypatch, {alpha: "sess-holder"})
    assert _rows(client)["sess-lead"]["status"] == "queued"

    stale = _post(client, "/api/tasks/queue/skip",
                  {"key": tasks_store.pending_key("e-lead")})
    assert stale.status_code == 404

    r = _post(client, "/api/tasks/queue/skip", {"entry_id": "e-follow"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "position": 1}
    assert priorities == [(["e-follow"], True)]


def test_skip_refuses_an_entry_id_that_names_nothing(client, folders, flag,
                                                     priorities):
    flag()
    assert _post(client, "/api/tasks/queue/skip",
                 {"entry_id": "no-such"}).status_code == 404
    assert _post(client, "/api/tasks/queue/skip", {}).status_code == 400
    assert priorities == []


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
    assert project_queue.held_answers() == []


def test_a_card_on_a_run_with_no_session_is_delivered_not_held(
        client, folders, monkeypatch, flag, agent, rings):
    """A held record is keyed by `session_id` — it is how delivery finds the
    row, how the chip finds its place and how Skip recognises the head of the
    line — so an empty one parks a decision no view can reach and rings the
    long-poll about nothing (`notify(None)`). A run with no session cannot be
    queued behind anything anyway; delivering now is the honest fallback."""
    flag()
    alpha, _beta = folders
    _holders(monkeypatch, {alpha: "sess-holder"})

    r = _post(client, "/api/tasks/queue/decide",
              _decide_body(alpha, session_id=""))
    assert r.status_code == 200, r.text
    assert r.json()["held"] is False
    assert len(agent.calls) == 1
    assert project_queue.held_answers() == []
    assert set() not in rings
