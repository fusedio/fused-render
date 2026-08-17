"""triage.json holds the user's FILING DECISIONS, not a cache of who is running.

The bug these tests exist for: `core_apps/sessions/inbox.html`'s `autoFlow`
wrote `{status: "in_progress"}` to triage.json for every session it saw running,
and only wrote it back to `done` when THAT SAME PAGE INSTANCE observed the stop
— it gated the clear-back on `RUNNING`, an in-memory `Set` that is empty on
every page load. So every run whose finish no Inbox tab was open to witness left
a pin on disk that nothing would ever clear, and the Tasks board honoured those
pins as the user's own act: five cards sat in In Progress for a day over runs
that had finished hours earlier.

`tasks.py::_pin_holds` now reaps an `in_progress` pin whose run has demonstrably
ended, which makes the stale pins harmless. It does not make them right, and it
is not the fix these tests pin. TWO writer-side rules are:

1. **Nothing mints a pin it cannot take back.** The automatic in-progress claim
   is not persisted at all, because it was never needed: `inbox.py` already
   DERIVES the lane from the session's own activity, and deriving it for a
   pinned session too covers the one case the write used to cover (a `done` or
   `archived` session that started running again). One rule, no pin — so an
   unwitnessed finish leaves nothing behind to be stale.

2. **A deliberate pin is stamped.** `set_triage.py` is the Inbox's writer and
   the shell's `/api/claude-sessions/triage` is the Board's; both now record
   `at`, because `_pin_holds` reads a stamp later than the session's last
   activity as "a decision no run has contradicted". Unstamped meant reapable,
   so without this the user pressing In Progress in the Inbox on purpose lost
   the choice on the next 20s poll.

Rule 1 is checked by running the page's REAL `autoFlow` under node — the
`_js_block` posture of test_sessions_inbox_layout.py / test_sessions_inbox_open_dir.py,
because a copy of the flow logic in the test would keep passing after the
shipping code regressed. Rules about disk are checked against the actual
modules, exec'd standalone the way the fused engine execs them.
"""
import datetime
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_APP = os.path.join(_ROOT, "core_apps", "sessions")


# --------------------------------------------------------------- loading

def _load(name):
    """A core_apps script exec'd standalone, the way runPython runs it."""
    path = os.path.join(_APP, name)
    spec = importlib.util.spec_from_file_location("core_" + name[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    # Both modules resolve their state dir off FUSED_RENDER_HOME at import time,
    # so this has to be set before `_load` — never the developer's real store.
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))


@pytest.fixture()
def triage_path(tmp_path):
    return tmp_path / "home" / "claude-sessions" / "triage.json"


@pytest.fixture()
def set_triage():
    return _load("set_triage.py")


@pytest.fixture()
def inbox(tmp_path, monkeypatch):
    """`inbox.py` with the transcript store pointed at tmp."""
    monkeypatch.syspath_prepend(os.path.join(_APP, "sessions"))
    sys.modules.pop("sessions", None)
    mod = _load("inbox.py")
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(mod._sessions, "PROJECTS_DIR", str(projects))
    monkeypatch.setattr(mod._sessions, "NAMES_FILE", str(tmp_path / "names.json"))
    mod.projects = projects
    yield mod
    sys.modules.pop("sessions", None)


def _transcript(inbox, session_id, *, quiet=False):
    """One session on disk, running or finished.

    Running = a real (non-housekeeping) entry timestamped inside the 45s window,
    which is what `_activity_mtime` reports and what `_is_running` tests. `quiet`
    is a finished run: an old entry, and an old file mtime so the tail read is
    skipped the way it is on a store full of yesterday's sessions.
    """
    d = inbox.projects / "-tmp-proj"
    d.mkdir(exist_ok=True)
    path = d / (session_id + ".jsonl")
    when = datetime.datetime.now(datetime.timezone.utc)
    if quiet:
        when -= datetime.timedelta(hours=1)
    path.write_text(json.dumps({
        "type": "user", "timestamp": when.isoformat().replace("+00:00", "Z"),
        "cwd": "/tmp/proj", "message": {"content": "hi"}}) + "\n",
        encoding="utf-8")
    if quiet:
        _quieten(path)
    return path


def _quieten(path):
    """Backdate the file, so the run reads as over. Separate from `_transcript`
    because one test has to watch a session STOP without rewriting it."""
    old = time.time() - 3600
    os.utime(path, (old, old))


def _pin(triage_path, record):
    triage_path.parent.mkdir(parents=True, exist_ok=True)
    triage_path.write_text(json.dumps(record), encoding="utf-8")


def _status(inbox, session_id):
    rows = {s["sessionId"]: s for s in inbox.main()["sessions"]}
    return rows[session_id]["status"]


# ------------------------------------------------- the page's real autoFlow

@pytest.fixture(scope="module")
def source():
    with open(os.path.join(_APP, "inbox.html"), encoding="utf-8") as f:
        return f.read()


def _auto_flow_block(src):
    """`RUNNING` and `autoFlow`, verbatim — the shipping flow logic."""
    start = src.find("const RUNNING = new Set();")
    assert start != -1, "inbox.html no longer keeps a RUNNING set"
    end = src.find("\n}", src.index("function autoFlow()", start))
    assert end != -1, "autoFlow is no longer a single function body"
    return src[start:end + 2]


def _flow(tmp_path, src, sessions, frames):
    """Drive `autoFlow` over a sequence of ticks and report what it SAVED.

    `sessions` is the rows as `inbox.py` handed them over (id + the status it
    derived); `frames` is one dict per tick of id -> "run"/"stop", which is what
    the status poll does to `s.mtime` in place. `saveMany` is stubbed to record
    the call AND to apply it locally, because the shipping one does (`applyLocal`)
    and a stub that didn't would hide a flow that depends on its own writes.
    """
    node = shutil.which("node")
    if not node:  # pragma: no cover - node is preinstalled on the CI runners
        pytest.skip("node is required to drive the page's JS")
    harness = tmp_path / "flow.mjs"
    harness.write_text(
        f"const sessions = {json.dumps(sessions)};\n"
        f"const frames = {json.dumps(frames)};\n"
        "const DATA = { sessions };\n"
        'function isRunning(m) { return m === "run"; }\n'
        "const calls = [];\n"
        "function saveMany(ids, patch) {\n"
        "  calls.push({ ids, patch });\n"
        "  for (const s of sessions)\n"
        "    if (ids.includes(s.sessionId) && patch.status !== undefined)\n"
        "      s.status = patch.status;\n"
        "}\n"
        f"{_auto_flow_block(src)}\n"
        "for (const frame of frames) {\n"
        '  for (const s of sessions) s.mtime = frame[s.sessionId] || "stop";\n'
        "  autoFlow();\n"
        "}\n"
        "console.log(JSON.stringify(calls));\n",
        encoding="utf-8")
    out = subprocess.run([node, str(harness)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def _pins(calls):
    """Just the `in_progress` writes — the ones that must not exist."""
    return [c for c in calls if c["patch"].get("status") == "in_progress"]


# ============================ 1. nothing mints a pin it cannot take back

def test_a_running_session_gets_no_pin_written_for_it(tmp_path, source):
    """The whole bug in one tick. The page sees a run, and writes nothing: the
    lane it wants is the one `inbox.py` already derived from the same activity,
    so the write bought a duplicate of a derived fact and a permanent lie in the
    file that outlived the run."""
    calls = _flow(tmp_path, source,
                  [{"sessionId": "s1", "status": "in_progress"}],
                  [{"s1": "run"}, {"s1": "run"}])
    assert _pins(calls) == []


def test_a_done_session_that_starts_running_still_gets_no_pin(tmp_path, source):
    """The one case the write used to earn its keep — a session with an older
    `done` record that got resumed, which `t.get("status") or default` would
    otherwise leave sitting in Done while its dot pulses. It is `inbox.py`'s job
    now (see the derivation tests below), so the write is still not needed."""
    calls = _flow(tmp_path, source, [{"sessionId": "s1", "status": "done"}],
                  [{"s1": "run"}])
    assert _pins(calls) == []


def test_an_unwitnessed_finish_writes_nothing_at_all(tmp_path, source):
    """A page that was never open while the session ran has nothing to say about
    it. This is the tick that used to be the ONLY thing keeping the file honest,
    and now there is nothing for it to clean up."""
    calls = _flow(tmp_path, source, [{"sessionId": "s1", "status": "done"}],
                  [{"s1": "stop"}, {"s1": "stop"}])
    assert calls == []


def test_a_witnessed_finish_still_files_the_run_as_unread_news(tmp_path, source):
    """The half of `autoFlow` that is a real observation rather than a cache: a
    run this page watched END is a fact, `done` is a timeless filing decision,
    and clearing `read` is how finished work surfaces in the Inbox as news."""
    calls = _flow(tmp_path, source, [{"sessionId": "s1", "status": "in_progress"}],
                  [{"s1": "run"}, {"s1": "stop"}])
    assert [c["patch"] for c in calls] == [{"status": "done", "read": ""}]
    assert calls[0]["ids"] == ["s1"]


def test_the_finish_is_noticed_even_when_the_row_was_loaded_as_done(
        tmp_path, source):
    """The regression that removing the pin write would otherwise cause.

    `s.status` is only recomputed by a full `load()`; the 5s poll updates
    `s.mtime` alone. So a session that was quiet at load and started running
    afterwards carries a stale `done` in `DATA`, and the finish branch used to
    reach that status only because the pin write had just set it locally. With
    the write gone the gate has to be what this page OBSERVED — `RUNNING` — and
    not the status the last scan happened to render."""
    calls = _flow(tmp_path, source, [{"sessionId": "s1", "status": "done"}],
                  [{"s1": "run"}, {"s1": "stop"}])
    assert [c["patch"] for c in calls] == [{"status": "done", "read": ""}]


def test_a_finish_is_filed_once_and_not_on_every_later_tick(tmp_path, source):
    """`RUNNING.delete` is what makes it once. A write per poll would rewrite
    `read: ""` forever and no session could ever be marked read."""
    calls = _flow(tmp_path, source, [{"sessionId": "s1", "status": "in_progress"}],
                  [{"s1": "run"}, {"s1": "stop"}, {"s1": "stop"}, {"s1": "stop"}])
    assert len(calls) == 1


# =========================== 2. the Inbox derives the lane instead of pinning

def test_a_running_session_is_in_progress_with_no_record_at_all(inbox):
    """Already true before this change, and it is the reason the pin was
    redundant: the untriaged default is derived from the session's activity."""
    _transcript(inbox, "s1")
    assert _status(inbox, "s1") == "in_progress"


def test_a_quiet_session_is_done_with_no_record_at_all(inbox, triage_path):
    """The other end of the derivation, and the answer an unwitnessed finish now
    gets for free. Nothing wrote a file to get here."""
    _transcript(inbox, "s1", quiet=True)
    assert _status(inbox, "s1") == "done"
    assert not triage_path.exists(), "the Inbox invented a record for a quiet run"


@pytest.mark.parametrize("pinned", ["done", "archived"])
def test_a_running_session_outranks_a_stale_filing_decision(
        inbox, triage_path, pinned):
    """A resumed session belongs in In Progress even though it was filed away
    earlier, because "running" is a fact about the present and the pin is a
    decision about a run that has since been superseded. `autoFlow` used to buy
    this by overwriting the record; the derivation buys it without touching disk,
    so the pin the user made is still there when the run stops."""
    _transcript(inbox, "s1")
    _pin(triage_path, {"s1": {"status": pinned}})
    assert _status(inbox, "s1") == "in_progress"


def test_the_filing_decision_comes_back_when_the_run_stops(inbox, triage_path):
    """Because it was never overwritten. This is the difference between deriving
    and caching: the record still says what the user said."""
    path = _transcript(inbox, "s1")
    _pin(triage_path, {"s1": {"status": "archived", "note": "keep me"}})
    assert _status(inbox, "s1") == "in_progress"
    _quieten(path)
    assert _status(inbox, "s1") == "archived"
    assert json.loads(triage_path.read_text())["s1"]["note"] == "keep me"


def test_a_deliberate_in_progress_pin_holds_on_a_quiet_session(inbox, triage_path):
    """The Inbox honours the pin it is given — the reap that tells a deliberate
    pin from an automatic one lives in the Tasks board, and after this change
    every `in_progress` in the file IS deliberate."""
    _transcript(inbox, "s1", quiet=True)
    _pin(triage_path, {"s1": {"status": "in_progress", "at": str(time.time())}})
    assert _status(inbox, "s1") == "in_progress"


def test_a_status_the_page_cannot_render_falls_back_to_the_derivation(
        inbox, triage_path):
    _transcript(inbox, "s1", quiet=True)
    _pin(triage_path, {"s1": {"status": "nonsense"}})
    assert _status(inbox, "s1") == "done"


# =============================== 3. the manual path stamps its pin

def test_a_status_write_from_the_inbox_is_stamped(set_triage, triage_path):
    """Every `in_progress` that reaches disk through here is now a person
    pressing In Progress — `autoFlow` no longer writes one — and `_pin_holds`
    reads the absence of a stamp as "older than anything that has happened", so
    an unstamped write would be reaped on the Board's next 20s poll."""
    before = time.time()
    out = set_triage.main("s1", json.dumps({"status": "in_progress"}))
    assert out["ok"] is True
    rec = json.loads(triage_path.read_text())["s1"]
    assert rec["status"] == "in_progress"
    # the shape `tasks.py::_pin_at` parses: a stringified epoch, like every
    # other field this writer coerces
    assert isinstance(rec["at"], str)
    assert float(rec["at"]) >= before


def test_the_stamp_is_the_servers_clock_and_not_the_pages(set_triage, triage_path):
    """`at` is not in `FIELDS`, so a patch cannot set it. The browser clock is
    the one thing that must not decide whether a pin outlives a run."""
    set_triage.main("s1", json.dumps({"status": "in_progress", "at": "1.0"}))
    rec = json.loads(triage_path.read_text())["s1"]
    assert float(rec["at"]) > 1.0


def test_a_note_edit_does_not_refresh_the_stamp(set_triage, triage_path):
    """The stamp answers "when was this STATUS chosen". Restamping on any write
    would let a note typed today resurrect a pin from a run that ended last
    week."""
    _pin(triage_path, {"s1": {"status": "in_progress", "at": "1.0"}})
    set_triage.main("s1", json.dumps({"note": "later thought"}))
    rec = json.loads(triage_path.read_text())["s1"]
    assert rec["at"] == "1.0"
    assert rec["note"] == "later thought"


def test_clearing_the_status_takes_the_stamp_with_it(set_triage, triage_path):
    """A record with no status has no pin to stamp, and an orphan `at` would
    keep the record non-empty — which is what returns a session to the Inbox
    pile (`if rec: ... else: pop`)."""
    _pin(triage_path, {"s1": {"status": "in_progress", "at": "1.0"}})
    set_triage.main("s1", json.dumps({"status": ""}))
    assert json.loads(triage_path.read_text()) == {}


def test_the_stamp_does_not_disturb_the_records_other_keys(set_triage,
                                                           triage_path):
    _pin(triage_path, {"s1": {"status": "done", "note": "n", "tags": "a,b",
                              "read": "1"}})
    set_triage.main("s1", json.dumps({"status": "in_progress"}))
    rec = json.loads(triage_path.read_text())["s1"]
    assert rec["note"] == "n" and rec["tags"] == "a,b" and rec["read"] == "1"
    assert set(rec) == {"status", "note", "tags", "read", "at"}


def _fields(src):
    """The writer's `FIELDS` set, read out of the source."""
    line = next(ln for ln in src.splitlines() if ln.startswith("FIELDS"))
    return {w.strip().strip('"\'') for w in
            line.split("{", 1)[1].split("}", 1)[0].split(",")}


def test_both_writers_of_triage_json_stamp_the_same_field():
    """A duplicated rule needs a test rather than a comment (D146): the Inbox
    writes triage.json directly through `set_triage.py` while the Board goes
    through `/api/claude-sessions/triage`, and a pin only survives if BOTH of
    them stamp the field `_pin_at` reads."""
    with open(os.path.join(_APP, "set_triage.py"), encoding="utf-8") as f:
        writer = f.read()
    router = os.path.join(_ROOT, "fused_render", "server", "routers",
                          "claude_sessions.py")
    with open(router, encoding="utf-8") as f:
        shell = f.read()
    for src, who in ((writer, "set_triage.py"), (shell, "claude_sessions.py")):
        assert '"at"' in src or "'at'" in src, f"{who} no longer stamps the pin"
    assert "at" not in _fields(writer), (
        "`at` became a patchable field: the page's clock must not set it")
