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

from fused_render import drafts, project_queue, schedule, tasks_store, tasks_watch
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
    monkeypatch.setattr(tasks_watch, "HISTORY_PATH",
                        str(tmp_path / "no-history.jsonl"))
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


def test_behind_names_the_task_directly_ahead_and_not_always_the_holder(
        client, projects_dir, folders, monkeypatch, flag):
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
    # "behind" NAMES THE TASK DIRECTLY AHEAD, and held answers are stepped over:
    # the answering task is behind the holder, the skipped message is behind the
    # holder too (the only thing in front of it is that held answer), and the
    # plain one is behind the skipped message.
    assert rows["sess-answer"]["queue_ahead"] == rows["sess-holder"]["task_id"]
    assert rows["sess-skipped"]["queue_ahead"] == rows["sess-holder"]["task_id"]
    assert rows["sess-plain"]["queue_ahead"] == rows["sess-skipped"]["task_id"]
    assert rows["sess-plain"]["queue_ahead_key"] == "sess-skipped"


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
    # The sidebar's rows are links too, and one message is waiting on this one.
    assert row["queue_ahead_session"] == "sess-holder"
    assert row["queue_waiting"] == 1
    # Compact stays compact: the folder, the holder's title and the holder's
    # own path are not here.
    assert "queue_key" not in row
    assert "queue_ahead_title" not in row
    assert "queue_ahead_target" not in row


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
        client, folders, flag, chat_run):
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
    assert _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "session_id": "", "message": "first"}
                 ).json() == {"run": True}
    assert project_queue.reserved(alpha) == ""
    # The run appears: alive, in that folder, and it has not named itself.
    chat_run("run-1", alpha, pid=os.getpid())
    project_queue.invalidate_holders()
    assert project_queue.holders()[alpha]["kind"] == "starting"
    # The session alone still cannot match it — that is the state the bug was
    # reported from, and what the run id is for.
    assert project_queue.is_free(alpha, "sess-new") is False

    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "sess-new", "run_id": "run-1",
               "message": "second"})
    assert r.status_code == 200, r.text
    assert r.json() == {"run": True}
    assert schedule.list_entries() == []          # nothing was queued
    # …and the reservation now carries the name the chat finally has.
    assert project_queue.reserved(alpha) == "sess-new"
    assert project_queue.reserved_run(alpha) == "run-1"


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

    held = project_queue.holders()[alpha]
    assert held["kind"] == "run" and held["session_id"] == "sess-new"
    # No run_id needed: the conversation is named now.
    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "sess-new", "message": "second"})
    assert r.json() == {"run": True}
    assert schedule.list_entries() == []


def test_a_run_id_does_not_admit_a_chat_into_somebody_elses_folder(
        client, folders, flag, chat_run):
    """The self-match is about identity, not a skeleton key: a run id that
    names nothing in this folder queues like anything else."""
    flag()
    alpha, _beta = folders
    chat_run("run-other", alpha, pid=os.getpid(), session_id="sess-holder")
    project_queue.reserve(alpha, "sess-holder")
    project_queue.invalidate_holders()

    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "sess-mine", "run_id": "run-9",
               "message": "me too"})
    assert r.status_code == 200, r.text
    assert r.json()["run"] is False
    assert _rows(client)["sess-mine"]["status"] == "queued"


def test_a_new_chats_second_message_is_not_queued_behind_its_own_teardown(
        client, folders, flag, chat_run, registry):
    """THE SELF-QUEUE WINDOW, over HTTP (browser QA, 2026-09-12). A brand-new
    chat said hello, got its reply, and the next message was queued behind its
    own conversation: the admission named neither the session nor the run (the
    client had not learned either yet), the first message's ANONYMOUS
    reservation was still standing, and the run was between turns.

    Both halves are asserted here because the difference between them is the
    whole rule: while the turn is genuinely running the nameless send queues,
    and the moment the registry says the turn is over it goes."""
    flag()
    alpha, _beta = folders
    assert _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "session_id": "", "message": "hello"}
                 ).json() == {"run": True}
    # The round trip that first message paid for: it was admitted, spawned,
    # answered and read before the user typed again, so the reservation standing
    # in the way is that old. A reservation younger than one round trip is the
    # OTHER case — a second chat pressing Enter in the same breath — and it is
    # what `project_queue.ANONYMOUS_CLAIM_AFTER` refuses.
    key = project_queue.queue_key(alpha)
    sid, expiry, run, taken = project_queue._reservations[key]
    project_queue._reservations[key] = (
        sid, expiry, run, taken - project_queue.ANONYMOUS_CLAIM_AFTER - 1)
    chat_run("run-1", alpha, pid=os.getpid())
    registry("sess-new", status="busy")
    project_queue.invalidate_holders()

    queued = _post(client, "/api/tasks/queue/admit",
                   {"project": alpha, "session_id": "", "message": "second"})
    assert queued.json()["run"] is False      # a turn IS running in there

    registry("sess-new", status="idle")       # …and now it is not
    project_queue.invalidate_holders()
    r = _post(client, "/api/tasks/queue/admit",
              {"project": alpha, "session_id": "", "message": "second"})
    assert r.status_code == 200, r.text
    assert r.json() == {"run": True}


def test_an_admitted_send_says_running_before_anything_has_spawned(
        client, projects_dir, folders, flag, rings):
    """INSTANT STATUS (browser QA, 2026-09-12: the sidebar took 3.4 s to say
    running, where the spec asks for two). Nothing on disk says a turn has
    started until `claude` registers a session — the chat spawns its own run, so
    there is no scheduler entry either — and the row went on reading the
    previous verdict for the whole of that gap. The reservation this endpoint
    takes is the one record of the instant, and the ring is what stops a page
    waiting out its own long-poll to hear about a send it just made."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha, "hello")
    assert _rows(client)["sess-a"]["status"] != "in_progress"

    assert _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "session_id": "sess-a", "message": "go"}
                 ).json() == {"run": True}
    assert {"sess-a"} in rings
    assert _rows(client)["sess-a"]["status"] == "in_progress"


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
    assert project_queue.reserved_sessions() == set()
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
    body = r.json()
    assert body["ok"] is True and body["position"] == 1
    # …AND WHO IS IN FRONT NOW (🟡 review, 2026-09-12), the same five fields
    # admit, decide and run-now answer with. The press has just changed this
    # line, and without them the chat could paint the claim but went on saying
    # "behind TASK-xxx" about whatever was ahead BEFORE it until the next
    # listing landed.
    assert body["ahead_key"] == "sess-holder"
    assert body["ahead_session"] == "sess-holder"
    assert body["ahead_target"] == alpha
    # `ahead` and `ahead_title` ride along too. The number is "" here because no
    # listing has minted one for the holder yet — exactly what `_queue_place`
    # answers for admit as well — and the title is the holder's own.
    assert body["ahead"] == "" and body["ahead_title"] == "go"
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
    # The same shape the ordinary road answers with, `ahead_*` and all — one
    # caller reads one answer.
    assert r.json()["ok"] is True and r.json()["position"] == 1
    assert "ahead_key" in r.json()
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
                        "ahead_title": "holding the folder",
                        "ahead_key": "sess-holder"}
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

    skipped = _post(client, "/api/tasks/queue/skip",
                    {"key": "sess-second"}).json()
    assert skipped["ok"] is True and skipped["position"] == 1

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
    assert body["ahead_session"] == "sess-holder"
    assert body["ahead_target"] == alpha
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
    # Run now IS Run next, and Run next is play next: the message just promoted
    # goes to the head, past sess-b's two older promotions.
    assert body["position"] == 1
    assert body["position"] == _rows(client)["sess-a"]["queue_position"]

    # …and when sess-b asks to go next again, its TWO entries are still ONE
    # slot: this row moves to #2, never to #3.
    schedule.set_priority(["e-b1", "e-b2"], True)
    assert _rows(client)["sess-a"]["queue_position"] == 2


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
    assert r.json()["ok"] is True and r.json()["position"] == 1

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
    assert r.json()["ok"] is True and r.json()["position"] == 1
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
        client, projects_dir, folders, monkeypatch, flag, chat_run, registry):
    """A brand-new chat's run holds its folder before it has said who it is:
    alive, in that tree, and anonymous. There is nothing to name and nothing to
    open — "behind a run in this folder" is the whole of what a reader can be
    told — and the line is re-derived on every listing, so the moment the CLI
    registers its session against its pid the row fills in by itself
    (`project_queue.run_sessions`, the pid spelling).

    The queued row is unchanged across the two: what it is waiting for never
    moved, only what can be said about it."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-holder", alpha, "holding the folder")
    schedule._write([_entry("e-wait", "run the report", alpha,
                            session_id="sess-wait")])
    chat_run("run-1", alpha, pid=os.getpid())
    project_queue.invalidate_holders()
    assert project_queue.holders()[alpha]["kind"] == "starting"

    row = _rows(client)["sess-wait"]
    assert row["status"] == "queued"
    assert row["queue_position"] == 1
    assert row["queue_ahead"] == ""
    assert row["queue_ahead_session"] == ""
    assert row["queue_ahead_target"] == ""

    # Seconds later: the registry names the run by its pid.
    registry("sess-holder", status="busy")
    # Only the holder memo: `tasks_mod.reset_cache()` would take the live
    # registry with it (it resets the watcher), which is the very thing that
    # has just named this run.
    project_queue.invalidate_holders()

    rows = _rows(client)
    row = rows["sess-wait"]
    assert row["status"] == "queued"
    assert row["queue_ahead"] == rows["sess-holder"]["task_id"]
    assert row["queue_ahead_session"] == "sess-holder"
    assert row["queue_ahead_target"] == alpha


def test_the_card_counts_every_message_waiting_and_not_the_ones_that_are_not(
        client, projects_dir, folders, monkeypatch, flag):
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
        client, projects_dir, folders, flag):
    """END TO END, through the endpoint that writes the word: the send the chat
    queued comes back as an ordinary pending entry, and the row it lands on does
    not ask its own composer to shut."""
    flag()
    alpha, _beta = folders
    _transcript(projects_dir, "sess-a", alpha, "holding the folder")
    _transcript(projects_dir, "sess-b", alpha, "me too")
    assert _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "session_id": "sess-a",
                  "message": "go"}).json() == {"run": True}

    body = _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "session_id": "sess-b",
                  "message": "me too"}).json()
    assert body["run"] is False
    assert body["entry"]["origin"] == "chat"

    row = _rows(client)["sess-b"]
    assert row["status"] == "queued"
    assert row["queue_waiting"] == 1
    assert row["queue_blocking"] is False



# ============ the number a queued chat is given, and the entry that opens it
#                                        (Akshil's findings B and C, 2026-09-12)


def test_a_queued_new_chat_is_named_the_moment_it_queues(
        client, projects_dir, folders, monkeypatch, flag):
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
    rows = _rows(client)
    assert rows[key]["task_id"] == number
    assert rows[key]["status"] == "queued"
    # …and the same key still answers the same number on every listing after
    # that: allocation is once, keyed by the task key (`ensure_ids`).
    assert _rows(client)[key]["task_id"] == number


def test_a_queued_send_carries_the_chat_drafts_name_and_takes_the_draft_with_it(
        client, projects_dir, folders, monkeypatch, flag):
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
    assert _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "message": "half a thought",
                  "draft_key": key}).json() == {"run": True}
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
    assert _post(client, "/api/tasks/queue/admit",
                 {"project": alpha, "session_id": "sess-a",
                  "message": "go"}).json() == {"run": True}
    assert tasks_store.task_ids() == {}


def test_a_pending_row_carries_the_entry_that_opens_it(
        client, projects_dir, folders, monkeypatch, flag):
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
