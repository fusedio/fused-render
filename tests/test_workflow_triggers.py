"""Programmatic triggers for the workflow canvas — the model
(fused_render/workflow_triggers.py).

Nothing here spawns a real `claude`, and nothing here executes the workflow
template: the single seam into it (`_template_run`) is stubbed, which is also
the point of that seam existing. What IS exercised for real is everything the
module owns — the store, arming and its fingerprint, the fingerprint check
before a run, the queue and its bound, the rate cap, disarm-on-repeated-error,
and the file sweep's idempotency across a restart.

The template's own `run.py` has no tests, deliberately (SPEC §46: the canvas is
a prototype and ships untested), which is exactly why the seam is where it is.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from fused_render import workflow_triggers as wt


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A per-test store. These tests assert on exact store contents."""
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    wt.reset()
    yield tmp_path / "home"
    wt.reset()


@pytest.fixture
def doc(tmp_path):
    """A workflow document on disk. Its CONTENT is irrelevant to this module —
    the stubbed runner decides what it compiles to — but it must exist, because
    `arm` refuses a path that is not a file."""
    path = tmp_path / "triage.workflow.json"
    path.write_text(json.dumps({"nodes": [], "edges": []}))
    return str(path)


class FakeRunner:
    """The workflow template's `run.py`, as far as this module can tell.

    It models the ONE part of `run.py`'s contract this module depends on for its
    safety property: `start` compiles the document itself and refuses when the
    result does not match the `approved` authorization it was handed. Nothing
    here checks the tool set on core's behalf, because `run.py` no longer lets
    it — that is the whole of finding 1's fix, and a fake that checked in the
    caller would test the bug rather than the code.

    `on_start` is the seam for the race: it runs INSIDE the start call, before
    the authorization is computed, so a test can make the document change at the
    exact moment a real save would have.
    """

    def __init__(self, tools=("mcp__mail__search_mail",)):
        self.tools = list(tools)
        self.servers = {}
        self.calls = []
        self.starts = []
        self.cancels = []
        self.finish = None      # None = still running
        self.start_ok = True
        self.start_raises = False
        self.on_start = None
        self.triggerInputs = []
        self._n = 0

    def _authorization(self):
        return {"tools": sorted(set(self.tools)),
                "servers": dict(self.servers)}

    def __call__(self, params):
        self.calls.append(dict(params))
        action = params.get("action")
        if action == "plan":
            return dict({"ok": True, "name": "Triage",
                         "triggerInputs": list(self.triggerInputs), "steps": []},
                        **self._authorization())
        if action == "start":
            # The document may change right here — that is the point of the hook.
            if self.on_start is not None:
                self.on_start()
            if self.start_raises:
                # What `_template_run` turns an executor timeout into: the spawn
                # may or may not have happened, and nobody can tell from here.
                return {"ok": False, "reason": "runner_failed",
                        "message": "TimeoutError: run_python timed out"}
            actual = self._authorization()
            approved = params.get("approved")
            if approved:
                if actual != {"tools": sorted(set(approved.get("tools") or [])),
                              "servers": dict(approved.get("servers") or {})}:
                    return {"ok": False, "reason": "tools_changed",
                            "message": "This workflow now calls %s, and what was "
                                       "approved was %s."
                                       % (", ".join(actual["tools"]),
                                          ", ".join(sorted(
                                              approved.get("tools") or [])))}
            if not self.start_ok:
                return {"ok": False, "reason": "spawn_failed",
                        "message": "could not start claude"}
            self._n += 1
            # `run.py` honours the id its caller names, which is what makes an
            # unanswered start call recoverable.
            run_id = str(params.get("runId") or "run-%d" % self._n)
            self.starts.append({"runId": run_id,
                                "payload": params.get("payload") or {},
                                "approved": params.get("approved"),
                                "path": params.get("path")})
            return dict({"ok": True, "runId": run_id, "nodes": []},
                        **self._authorization())
        if action == "cancel":
            self.cancels.append(params.get("runId"))
            return {"ok": True, "cancelled": True}
        if action == "poll":
            known = any(s["runId"] == params.get("runId") for s in self.starts)
            if not known:
                return {"ok": False, "reason": "unknown_run",
                        "message": "No run %r." % (params.get("runId"),)}
            if self.finish is None:
                return {"ok": True, "done": False, "error": "", "nodes": []}
            return dict({"ok": True, "done": True, "error": "", "nodes": [],
                         "summary": "did the thing"}, **self.finish)
        return {"ok": False, "reason": "unknown_action", "message": action}


@pytest.fixture
def runner(monkeypatch):
    fake = FakeRunner()
    monkeypatch.setattr(wt, "_template_run", fake)
    return fake


def _cron_every_minute():
    return [{"id": "t1", "kind": "schedule", "cron": "* * * * *"}]


# --------------------------------------------------------------- fingerprint


def test_fingerprint_is_about_the_set_not_the_order():
    a = wt.fingerprint(["mcp__x__b", "mcp__x__a"])
    assert a == wt.fingerprint(["mcp__x__a", "mcp__x__b", "mcp__x__a"])
    assert a != wt.fingerprint(["mcp__x__a"])
    assert a != wt.fingerprint(["mcp__x__a", "mcp__x__b", "mcp__x__c"])


def test_fingerprint_cannot_be_spliced():
    """Two different sets must not hash equal by concatenation."""
    assert wt.fingerprint(["ab", "c"]) != wt.fingerprint(["a", "bc"])


# -------------------------------------------------------------------- arming


def test_arm_records_the_tool_list_that_was_approved(doc, runner):
    out = wt.arm(doc, tools=["mcp__mail__search_mail"],
                 triggers=_cron_every_minute())
    assert out["ok"], out
    wf = wt.get(doc)
    assert wf["armed"] is True
    assert wf["tools"] == ["mcp__mail__search_mail"]
    assert wf["fingerprint"] == wt.fingerprint(["mcp__mail__search_mail"])


def test_arm_refuses_when_the_shown_list_is_not_the_real_one(doc, runner):
    """The list the human saw IS the approval. If the document changed under the
    dialog, arming must refuse rather than approve a list nobody read."""
    runner.tools = ["mcp__mail__search_mail", "mcp__mail__send_mail"]
    out = wt.arm(doc, tools=["mcp__mail__search_mail"],
                 triggers=_cron_every_minute())
    assert not out["ok"]
    assert out["reason"] == "tools_changed"
    assert "send_mail" in out["message"]
    assert wt.get(doc) is None


def test_arm_requires_a_tool_list_at_all(doc, runner):
    out = wt.arm(doc, triggers=_cron_every_minute())
    assert not out["ok"] and out["reason"] == "no_tool_list"


def test_arm_refuses_a_document_that_cannot_compile(doc, monkeypatch):
    monkeypatch.setattr(wt, "_template_run", lambda p: {
        "ok": False, "reason": "unresolved", "message": "step 1 names no app"})
    out = wt.arm(doc, tools=[], triggers=_cron_every_minute())
    assert not out["ok"] and out["reason"] == "unresolved"


def test_arm_refuses_a_bad_cron_line(doc, runner):
    out = wt.arm(doc, tools=["mcp__mail__search_mail"],
                 triggers=[{"id": "t1", "kind": "schedule", "cron": "nope"}])
    assert not out["ok"] and out["reason"] == "bad_trigger"


def test_arm_refuses_with_no_triggers(doc, runner):
    out = wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=[])
    assert not out["ok"] and out["reason"] == "bad_trigger"


def test_arm_refuses_a_watched_folder_that_is_not_one(doc, runner, tmp_path):
    out = wt.arm(doc, tools=["mcp__mail__search_mail"],
                 triggers=[{"id": "t1", "kind": "file",
                            "folder": str(tmp_path / "nope")}])
    assert not out["ok"] and out["reason"] == "bad_trigger"


# ---------------------------------------------------------------- revocation


def test_disarm_is_immediate_and_drops_queued_work(doc, runner):
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1})
    wt.enqueue(doc, {"a": 2})
    assert len(wt.get(doc)["queue"]) == 2

    assert wt.disarm(doc)["ok"]
    wf = wt.get(doc)
    assert wf["armed"] is False
    assert wf["queue"] == []
    # And nothing starts on the next tick.
    assert wt.tick() == []


def test_enqueue_refuses_a_workflow_that_is_not_armed(doc, runner):
    assert wt.enqueue(doc, {"a": 1})["reason"] == "not_armed"
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.disarm(doc)
    assert wt.enqueue(doc, {"a": 1})["reason"] == "not_armed"


def test_forget_drops_the_row(doc, runner):
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    assert wt.forget(doc)["ok"]
    assert wt.get(doc) is None
    assert wt.forget(doc)["reason"] == "unknown_workflow"


# ---------------------------------------------------- the re-arming refusal


def test_a_new_tool_refuses_the_run_and_demands_re_arming(doc, runner):
    """THE safety property. A node added to an armed workflow must not run with
    a tool set the human never approved."""
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1})

    runner.tools = ["mcp__mail__search_mail", "mcp__mail__list_accounts"]
    started = wt.tick()

    assert started == []
    assert not runner.starts
    wf = wt.get(doc)
    assert wf["armed"] is False
    assert wf["needs_rearm"] is True
    assert "list_accounts" in wf["needs_rearm_reason"]
    assert wf["queue"] == []
    # Not counted as a failed run: nothing ran.
    assert wf["consecutive_errors"] == 0
    assert wf["runs"] == []
    assert any(e["kind"] == wt.EVENT_REFUSED for e in wt.event_log())


def test_removing_a_tool_also_needs_re_arming(doc, runner):
    """A SMALLER set is still a different set. The approval was for a list, and
    'fewer tools is fine' would make the fingerprint an inequality nobody
    reviewed — including the case where one tool is swapped for another."""
    runner.tools = ["mcp__mail__search_mail", "mcp__mail__list_accounts"]
    wt.arm(doc, tools=["mcp__mail__search_mail", "mcp__mail__list_accounts"],
           triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1})
    runner.tools = ["mcp__mail__search_mail"]

    assert wt.tick() == []
    assert wt.get(doc)["needs_rearm"] is True


def test_re_arming_after_a_tool_change_clears_the_flag(doc, runner):
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1})
    runner.tools = ["mcp__mail__search_mail", "mcp__mail__list_accounts"]
    wt.tick()
    assert wt.get(doc)["needs_rearm"] is True

    out = wt.arm(doc, tools=sorted(runner.tools), triggers=_cron_every_minute())
    assert out["ok"]
    wf = wt.get(doc)
    assert wf["armed"] and not wf["needs_rearm"]
    assert wf["fingerprint"] == wt.fingerprint(runner.tools)


def test_an_unchanged_tool_set_runs(doc, runner):
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1}, source="manual")
    started = wt.tick()
    assert len(started) == 1
    assert runner.starts[0]["payload"] == {"a": 1}
    # The id was chosen by CORE and honoured by the runner, which is what
    # makes an unanswered start call recoverable (see the spawn-grace tests).
    assert wt.get(doc)["current"]["runId"] == runner.starts[0]["runId"]


# ------------------------------------------------------------- concurrency


def test_one_run_at_a_time_and_the_rest_queue(doc, runner):
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"n": 1})
    wt.enqueue(doc, {"n": 2})
    wt.enqueue(doc, {"n": 3})

    assert len(wt.tick()) == 1
    assert len(runner.starts) == 1
    # The run has not finished, so the next two ticks start nothing.
    assert wt.tick() == []
    assert wt.tick() == []
    assert len(wt.get(doc)["queue"]) == 2

    runner.finish = {"done": True}
    assert len(wt.tick()) == 1          # the finish is cleared, the next starts
    assert len(runner.starts) == 2
    assert runner.starts[1]["payload"] == {"n": 2}


def test_the_queue_is_bounded_and_the_drops_are_counted(doc, runner):
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    for n in range(wt.QUEUE_MAX + 5):
        wt.enqueue(doc, {"n": n})
    wf = wt.get(doc)
    assert len(wf["queue"]) == wt.QUEUE_MAX
    assert wf["dropped"] == 5
    # The OLDEST went: the newest event is the one still worth acting on.
    assert wf["queue"][0]["payload"] == {"n": 5}


# ---------------------------------------------------------------- rate cap


def test_the_rate_cap_holds_runs_back_without_dropping_them(doc, runner):
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute(),
           max_runs_per_hour=2)
    for n in range(4):
        wt.enqueue(doc, {"n": n})
    runner.finish = {"done": True}

    assert len(wt.tick()) == 1
    assert len(wt.tick()) == 1
    assert wt.tick() == []              # capped
    assert len(runner.starts) == 2
    # Held, not dropped.
    assert len(wt.get(doc)["queue"]) == 2
    assert wt.get(doc)["dropped"] == 0


def test_the_rate_window_rolls(doc, runner, monkeypatch):
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute(),
           max_runs_per_hour=1)
    wt.enqueue(doc, {"n": 1})
    wt.enqueue(doc, {"n": 2})
    runner.finish = {"done": True}
    assert len(wt.tick()) == 1
    assert wt.tick() == []

    # An hour goes by: the window empties and the held event goes.
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 3601)
    assert len(wt.tick()) == 1


def test_the_default_rate_cap_is_used_when_none_is_given(doc, runner):
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    assert wt.get(doc)["max_runs_per_hour"] == wt.DEFAULT_RUNS_PER_HOUR
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute(),
           max_runs_per_hour=0)
    assert wt.get(doc)["max_runs_per_hour"] == wt.DEFAULT_RUNS_PER_HOUR


# ------------------------------------------------------- error disarming


def test_repeated_failures_disarm_the_workflow(doc, runner):
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute(),
           error_limit=2)
    runner.finish = {"done": True, "error": "the mail server said no"}
    for n in range(4):
        wt.enqueue(doc, {"n": n})

    wt.tick()                            # start #1
    wt.tick()                            # poll -> error, start #2
    assert wt.get(doc)["consecutive_errors"] == 1
    wt.tick()                            # poll -> error #2 -> disarm
    wf = wt.get(doc)
    assert wf["armed"] is False
    assert wf["needs_rearm"] is True
    assert "2 runs in a row failed" in wf["needs_rearm_reason"]
    assert wf["queue"] == []
    assert any(e["kind"] == wt.EVENT_DISARMED for e in wt.event_log())


def test_a_success_resets_the_error_count(doc, runner):
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute(),
           error_limit=2)
    for n in range(4):
        wt.enqueue(doc, {"n": n})
    runner.finish = {"done": True, "error": "boom"}
    wt.tick()
    wt.tick()
    assert wt.get(doc)["consecutive_errors"] == 1
    runner.finish = {"done": True, "error": ""}
    wt.tick()
    assert wt.get(doc)["consecutive_errors"] == 0
    assert wt.get(doc)["armed"] is True


def test_a_failed_step_is_a_failed_run(doc, runner):
    """A run whose `result` row is fine but whose step errored is not a success —
    the readout attributes failure per node, and so must the counter."""
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute(),
           error_limit=5)
    wt.enqueue(doc, {"n": 1})
    wt.tick()
    runner.finish = {"done": True, "error": "",
                     "nodes": [{"id": "n1", "label": "Find mail",
                                "status": "error", "error": "no such folder"}]}
    wt.tick()
    wf = wt.get(doc)
    assert wf["consecutive_errors"] == 1
    assert wf["runs"][-1]["state"] == "error"
    assert "no such folder" in wf["runs"][-1]["detail"]


def test_a_spawn_that_fails_is_recorded_and_counted(doc, runner):
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"n": 1})
    runner.start_ok = False
    assert wt.tick() == []
    wf = wt.get(doc)
    assert wf["current"] is None
    assert wf["runs"][-1]["state"] == "error"
    assert wf["consecutive_errors"] == 1


# --------------------------------------------------------- schedule triggers


def test_a_schedule_trigger_fires_when_its_time_comes(doc, runner):
    wt.arm(doc, tools=["mcp__mail__search_mail"],
           triggers=[{"id": "hourly", "kind": "schedule", "cron": "0 * * * *"}])
    now = datetime.now(timezone.utc)
    assert wt.tick(now) == []                      # not due yet

    started = wt.tick(now + timedelta(hours=2))
    assert len(started) == 1
    assert started[0]["source"] == "schedule:hourly"
    assert started[0]["payload"]["trigger"] == "hourly"


def test_a_missed_schedule_backlog_coalesces_to_one_run(doc, runner):
    """schedule.py's rule, and for its reason: replaying a week of 'every hour'
    into a workflow with real tools behind it is not what the words meant."""
    wt.arm(doc, tools=["mcp__mail__search_mail"],
           triggers=[{"id": "hourly", "kind": "schedule", "cron": "0 * * * *"}])
    runner.finish = {"done": True}
    late = datetime.now(timezone.utc) + timedelta(days=7)

    assert len(wt.tick(late)) == 1
    # One event, not 168 — the queue is empty behind it.
    assert wt.get(doc)["queue"] == []
    assert wt.get(doc)["dropped"] == 0


def test_a_structured_recurrence_also_fires(doc, runner):
    anchor = (datetime.now().replace(microsecond=0)
              - timedelta(days=1)).isoformat()
    out = wt.arm(doc, tools=["mcp__mail__search_mail"],
                 triggers=[{"id": "daily", "kind": "schedule",
                            "rule": {"freq": "day", "interval": 1},
                            "anchor": anchor}])
    assert out["ok"], out
    started = wt.tick(datetime.now(timezone.utc) + timedelta(days=3))
    assert len(started) == 1
    assert started[0]["source"] == "schedule:daily"


# ------------------------------------------------------------ file triggers


@pytest.fixture
def drop(tmp_path):
    folder = tmp_path / "drop"
    folder.mkdir()
    return folder


def _old(path, seconds=60):
    """Backdate a file past SETTLE_S so this tick's sweep sees it."""
    when = time.time() - seconds
    os.utime(path, (when, when))


def test_a_dropped_file_starts_a_run_carrying_its_path(doc, runner, drop):
    wt.arm(doc, tools=["mcp__mail__search_mail"],
           triggers=[{"id": "inbox", "kind": "file", "folder": str(drop)}])
    target = drop / "invoice.csv"
    target.write_text("a,b\n1,2\n")
    _old(target)

    started = wt.tick()
    assert len(started) == 1
    payload = started[0]["payload"]
    assert payload["path"] == os.path.abspath(str(target))
    assert payload["name"] == "invoice.csv"
    assert payload["ext"] == "csv"
    # AGAINST THE FILE, NOT AGAINST THE STRING THAT WAS WRITTEN. The payload's
    # `size` comes from `st_size` and describes the bytes that actually landed;
    # a text-mode write translates each "\n" to "\r\n" on Windows, so the
    # string's length is not the file's length there. Asserting `len(...)`
    # tested the platform rather than the payload.
    assert payload["size"] == os.path.getsize(target)
    assert payload["size"] > 0
    assert started[0]["source"] == "file:inbox"


def test_the_same_file_never_triggers_twice(doc, runner, drop):
    wt.arm(doc, tools=["mcp__mail__search_mail"],
           triggers=[{"id": "inbox", "kind": "file", "folder": str(drop)}])
    target = drop / "a.txt"
    target.write_text("x")
    _old(target)
    runner.finish = {"done": True}

    assert len(wt.tick()) == 1
    assert wt.tick() == []
    assert wt.tick() == []
    assert len(runner.starts) == 1


def test_idempotency_survives_a_restart(doc, runner, drop, monkeypatch):
    """The processed-file marker is DURABLE. A server restart must not re-fire a
    folder full of files somebody already dealt with."""
    wt.arm(doc, tools=["mcp__mail__search_mail"],
           triggers=[{"id": "inbox", "kind": "file", "folder": str(drop)}])
    target = drop / "a.txt"
    target.write_text("x")
    _old(target)
    runner.finish = {"done": True}
    assert len(wt.tick()) == 1

    # "Restart": every scrap of in-memory state goes, the store stays.
    wt.reset()
    monkeypatch.setattr(wt, "_template_run", runner)
    assert wt.tick() == []
    assert len(runner.starts) == 1


def test_a_changed_file_fires_again(doc, runner, drop):
    wt.arm(doc, tools=["mcp__mail__search_mail"],
           triggers=[{"id": "inbox", "kind": "file", "folder": str(drop)}])
    target = drop / "a.txt"
    target.write_text("x")
    _old(target)
    runner.finish = {"done": True}
    assert len(wt.tick()) == 1

    target.write_text("xy")
    _old(target)
    assert len(wt.tick()) == 1
    assert len(runner.starts) == 2


def test_arming_does_not_fire_on_what_is_already_there(doc, runner, drop):
    for n in range(5):
        target = drop / ("old%d.txt" % n)
        target.write_text("x")
        _old(target)
    wt.arm(doc, tools=["mcp__mail__search_mail"],
           triggers=[{"id": "inbox", "kind": "file", "folder": str(drop)}])
    assert wt.tick() == []
    assert len(wt.get(doc)["seen"]) == 5


def test_the_sweep_ignores_editor_noise_and_dot_dirs(doc, runner, drop):
    wt.arm(doc, tools=["mcp__mail__search_mail"],
           triggers=[{"id": "inbox", "kind": "file", "folder": str(drop),
                      "recursive": True}])
    for name in (".a.txt.swp", ".DS_Store", "a.txt.tmp", "b.txt~",
                 "c.crdownload", "~$doc.docx"):
        target = drop / name
        target.write_text("x")
        _old(target)
    git = drop / ".git"
    git.mkdir()
    (git / "HEAD").write_text("ref")
    _old(git / "HEAD")
    nm = drop / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("x")
    _old(nm / "index.js")

    assert wt.tick() == []
    assert wt.get(doc)["seen"] == {}


def test_a_glob_narrows_what_counts_as_an_arrival(doc, runner, drop):
    wt.arm(doc, tools=["mcp__mail__search_mail"],
           triggers=[{"id": "inbox", "kind": "file", "folder": str(drop),
                      "match": "*.csv"}])
    for name in ("a.csv", "b.txt"):
        target = drop / name
        target.write_text("x")
        _old(target)
    started = wt.tick()
    assert len(started) == 1
    assert started[0]["payload"]["name"] == "a.csv"


def test_a_file_still_being_written_waits_a_tick(doc, runner, drop):
    wt.arm(doc, tools=["mcp__mail__search_mail"],
           triggers=[{"id": "inbox", "kind": "file", "folder": str(drop)}])
    target = drop / "big.bin"
    target.write_text("partial")          # mtime is now
    assert wt.tick() == []
    _old(target)
    assert len(wt.tick()) == 1


def test_a_recursive_watch_sees_subfolders_and_a_flat_one_does_not(
        doc, runner, drop):
    sub = drop / "sub"
    sub.mkdir()
    target = sub / "a.txt"
    target.write_text("x")
    _old(target)

    wt.arm(doc, tools=["mcp__mail__search_mail"],
           triggers=[{"id": "inbox", "kind": "file", "folder": str(drop)}])
    assert wt.tick() == []

    wt.forget(doc)
    wt.arm(doc, tools=["mcp__mail__search_mail"],
           triggers=[{"id": "inbox", "kind": "file", "folder": str(drop),
                      "recursive": True}])
    # Armed AFTER the file existed, so it was seeded — a new one fires.
    target2 = sub / "b.txt"
    target2.write_text("x")
    _old(target2)
    started = wt.tick()
    assert len(started) == 1
    assert started[0]["payload"]["name"] == "b.txt"


# --------------------------------------------------------------- provenance


def test_a_run_records_where_it_came_from(doc, runner, drop):
    wt.arm(doc, tools=["mcp__mail__search_mail"],
           triggers=[{"id": "inbox", "kind": "file", "folder": str(drop)}])
    target = drop / "a.txt"
    target.write_text("x")
    _old(target)
    runner.finish = {"done": True}
    wt.tick()
    wt.tick()
    run = wt.get(doc)["runs"][-1]
    assert run["source"] == "file:inbox"
    assert run["payload"]["name"] == "a.txt"
    assert run["state"] == "done"
    assert run["runId"] == runner.starts[0]["runId"]


def test_history_is_bounded(doc, runner):
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    runner.finish = {"done": True}
    for n in range(wt.RUNS_KEPT + 10):
        wt.enqueue(doc, {"n": n})
        wt.tick()
    assert len(wt.get(doc)["runs"]) <= wt.RUNS_KEPT


# ------------------------------------------------------------------ listing


def test_the_listing_keeps_disarmed_workflows_and_puts_armed_ones_first(
        doc, runner, tmp_path):
    other = tmp_path / "other.workflow.json"
    other.write_text("{}")
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.arm(str(other), tools=["mcp__mail__search_mail"],
           triggers=_cron_every_minute())
    wt.disarm(doc)
    rows = wt.list_workflows()
    assert [r["armed"] for r in rows] == [True, False]
    assert {r["path"] for r in rows} == {doc, str(other)}


def test_a_symlinked_path_is_the_same_workflow(doc, runner, tmp_path):
    link = tmp_path / "link.workflow.json"
    try:
        os.symlink(doc, link)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    assert wt.get(str(link)) is not None
    assert len(wt.list_workflows()) == 1


# ============================================================ the review fixes


# ---------------------------------------- 1: one compile, one decision (HIGH)


def test_the_approved_set_travels_with_the_start_call(doc, runner):
    """Core must not check the tool set itself — it hands the approval to the
    process that builds `--allowed-tools`, so there is one compile to disagree
    with."""
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1})
    wt.tick()

    assert runner.starts[0]["approved"] == {
        "tools": ["mcp__mail__search_mail"], "servers": {}}
    # And no separate compile was made to check against: the only `plan` call in
    # the whole exchange is the one `arm` made.
    assert [c["action"] for c in runner.calls].count("plan") == 1


def test_a_document_saved_during_the_start_call_cannot_slip_through(doc, runner):
    """THE RACE finding 1 is about.

    The document changes at the instant the runner compiles it — later than any
    check core could have made, and exactly where a canvas Save, an editor or a
    sync client would land. The run must still refuse, because the comparison
    happens against the compile that becomes `--allowed-tools` and not against
    an earlier reading of the file.
    """
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1})

    def save_lands_now():
        runner.tools = ["mcp__mail__search_mail", "mcp__mail__list_accounts"]

    runner.on_start = save_lands_now
    started = wt.tick()

    assert started == []
    assert not runner.starts                 # nothing spawned
    wf = wt.get(doc)
    assert wf["armed"] is False
    assert wf["needs_rearm"] is True
    assert "list_accounts" in wf["needs_rearm_reason"]
    assert wf["queue"] == []
    assert wf["consecutive_errors"] == 0     # nothing ran, so nothing failed
    assert wf["runs"] == []
    assert any(e["kind"] == wt.EVENT_REFUSED for e in wt.event_log())


def test_a_refused_start_gives_back_its_rate_slot(doc, runner):
    """A claim that never became a run must not spend the hour's budget."""
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute(),
           max_runs_per_hour=2)
    wt.enqueue(doc, {"a": 1})
    runner.tools = ["mcp__mail__search_mail", "mcp__mail__list_accounts"]
    wt.tick()
    assert wt.get(doc)["rate_window"] == []


# ------------------------------------------- 12: the server map is authorized


def test_two_folders_sharing_a_basename_are_not_the_same_approval(doc, runner):
    """`mail`/`mail-2` are assigned in node order, so the tool NAMES survive a
    reorder that changes which account is reachable. The server map is what
    catches it."""
    runner.tools = ["mcp__mail__search_mail", "mcp__mail-2__search_mail"]
    runner.servers = {"mail": "/showcase/mail", "mail-2": "/work/mail"}
    assert wt.arm(doc, tools=sorted(runner.tools),
                  triggers=_cron_every_minute())["ok"]
    wt.enqueue(doc, {"a": 1})

    # The nodes are reordered: same names, swapped folders.
    runner.servers = {"mail": "/work/mail", "mail-2": "/showcase/mail"}
    assert wt.tick() == []
    assert not runner.starts
    assert wt.get(doc)["needs_rearm"] is True


def test_the_fingerprint_covers_the_server_map():
    a = wt.fingerprint(["mcp__mail__x"], {"mail": "/one"})
    assert a != wt.fingerprint(["mcp__mail__x"], {"mail": "/two"})
    assert a == wt.fingerprint({"tools": ["mcp__mail__x"],
                                "servers": {"mail": "/one"}})


# --------------------------------- 2: disarm during the start call stops it


def test_disarm_while_a_run_is_starting_cancels_it(doc, runner):
    """WC-12d says nothing starts behind a disarm. The spawn is already away by
    the time the click lands, so the run is cancelled rather than wished away."""
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1})

    def disarm_lands_now():
        wt.disarm(doc)

    runner.on_start = disarm_lands_now
    started = wt.tick()

    assert started == []
    assert len(runner.starts) == 1                    # it did spawn…
    assert runner.cancels == [runner.starts[0]["runId"]]   # …and was cancelled
    wf = wt.get(doc)
    assert wf["current"] is None
    assert wf["runs"][-1]["state"] == "cancelled"
    # Not a failure of the workflow — a revocation by its owner.
    assert wf["consecutive_errors"] == 0


def test_re_arming_while_a_run_is_starting_also_stops_it(doc, runner):
    """A re-arm is a new decision, and work decided under the old one must not
    land under it."""
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1})

    def rearm_lands_now():
        wt.arm(doc, tools=["mcp__mail__search_mail"],
               triggers=_cron_every_minute())

    runner.on_start = rearm_lands_now
    assert wt.tick() == []
    assert runner.cancels == [runner.starts[0]["runId"]]
    assert wt.get(doc)["runs"][-1]["state"] == "cancelled"


def test_a_normal_run_is_not_cancelled(doc, runner):
    """The generation guard must not fire when nothing happened."""
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1})
    assert len(wt.tick()) == 1
    assert runner.cancels == []
    assert wt.get(doc)["current"] is not None


# ------------------------- 3: an infrastructure failure must not disarm


def test_a_runner_failure_does_not_disarm_the_workflow(doc, runner):
    """An executor timeout is not evidence that the tool set changed. Only
    `tools_changed` disarms; everything else is the error counter's business."""
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1})
    runner.start_raises = True

    assert wt.tick() == []
    wf = wt.get(doc)
    assert wf["armed"] is True
    assert wf["needs_rearm"] is False
    assert wf["needs_rearm_reason"] == ""


def test_a_real_refusal_still_counts_as_a_failed_run(doc, runner):
    """`spawn_failed` is a real answer — nothing is running — so the error
    counter, which is the brake for a broken workflow, must see it."""
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1})
    runner.start_ok = False
    assert wt.tick() == []
    wf = wt.get(doc)
    assert wf["consecutive_errors"] == 1
    assert wf["runs"][-1]["state"] == "error"
    assert wf["armed"] is True


# ------------------------- 11: an unanswered start must not double-run


def test_an_unanswered_start_holds_its_claim_instead_of_starting_twice(
        doc, runner):
    """`run.py` detaches the session before it answers, so a killed start call
    may well have spawned. Clearing the claim would put a second session beside
    the first."""
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1})
    wt.enqueue(doc, {"a": 2})
    runner.start_raises = True

    assert wt.tick() == []
    wf = wt.get(doc)
    assert wf["current"] is not None
    assert wf["current"]["spawn_unknown"] is True
    assert wf["current"]["runId"]              # named before the call
    # The next tick must NOT drain the second event behind it.
    runner.start_raises = False
    assert wt.tick() == []
    assert runner.starts == []
    assert len(wt.get(doc)["queue"]) == 1


def test_a_held_claim_is_released_once_the_grace_expires(doc, runner,
                                                         monkeypatch):
    """If the spawn provably never happened, the workflow must not stay wedged."""
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1})
    runner.start_raises = True
    wt.tick()
    assert wt.get(doc)["current"] is not None
    # Something waiting behind it, so "the queue then moves" is observable.
    wt.enqueue(doc, {"a": 2})

    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + wt._SPAWN_GRACE_S + 5)
    runner.start_raises = False
    started = wt.tick()
    wf = wt.get(doc)
    # The stranded claim is released as `lost` — which is not a failure, because
    # "we could not tell" is not evidence the workflow is broken — and the queue
    # then moves.
    assert any(r["state"] == "lost" for r in wf["runs"])
    assert wf["consecutive_errors"] == 0
    assert len(started) == 1


def test_a_held_claim_that_did_spawn_is_polled_normally(doc, runner):
    """The other half: the run DID start, the call just died. `poll` finds it by
    the id core chose, and it completes as an ordinary run."""
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1})
    wt.tick()
    run_id = wt.get(doc)["current"]["runId"]

    # Pretend the answer never came back for a run that is nonetheless alive.
    with wt._lock:
        flows = wt._read()
        flows[wt.key_for(doc)]["current"]["spawn_unknown"] = True
        wt._write(flows)

    runner.finish = {"done": True}
    wt.tick()
    wf = wt.get(doc)
    assert wf["runs"][-1]["state"] == "done"
    assert wf["runs"][-1]["runId"] == run_id


# ------------------------------------------------- 4: a bounded recurrence


def test_a_recurrence_count_is_enforced(doc, runner):
    anchor = (datetime.now().replace(microsecond=0)
              - timedelta(days=10)).isoformat()
    out = wt.arm(doc, tools=["mcp__mail__search_mail"],
                 triggers=[{"id": "thrice", "kind": "schedule",
                            "rule": {"freq": "day", "interval": 1, "count": 3},
                            "anchor": anchor}])
    assert out["ok"], out
    runner.finish = {"done": True}

    now = datetime.now(timezone.utc)
    for day in range(1, 8):
        wt.tick(now + timedelta(days=day))
    assert len(runner.starts) == 3
    assert wt.get(doc)["made"]["thrice"] == 3
    assert wt.get(doc)["next_due"] == {}


def test_re_arming_restarts_a_bounded_recurrence(doc, runner):
    anchor = (datetime.now().replace(microsecond=0)
              - timedelta(days=10)).isoformat()
    spec = [{"id": "once", "kind": "schedule",
             "rule": {"freq": "day", "interval": 1, "count": 1},
             "anchor": anchor}]
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=spec)
    runner.finish = {"done": True}
    now = datetime.now(timezone.utc)
    wt.tick(now + timedelta(days=1))
    wt.tick(now + timedelta(days=2))
    assert len(runner.starts) == 1

    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=spec)
    assert wt.get(doc)["made"] == {}
    wt.tick(now + timedelta(days=3))
    assert len(runner.starts) == 2


# ------------------------------- 5: arming an unsatisfiable input is refused


def test_arm_refuses_an_input_no_trigger_can_supply(doc, runner):
    runner.triggerInputs = [{"step": "n1", "label": "Scan", "name": "path",
                             "key": "path", "kind": "str"}]
    out = wt.arm(doc, tools=["mcp__mail__search_mail"],
                 triggers=_cron_every_minute())
    assert not out["ok"]
    assert out["reason"] == "unsatisfiable_input"
    assert "'path'" in out["message"] and "schedule" in out["message"]
    assert wt.get(doc) is None


def test_arm_accepts_an_input_the_trigger_does_supply(doc, runner, tmp_path):
    folder = tmp_path / "drop"
    folder.mkdir()
    runner.triggerInputs = [{"step": "n1", "label": "Scan", "name": "path",
                             "key": "dir", "kind": "str"}]
    out = wt.arm(doc, tools=["mcp__mail__search_mail"],
                 triggers=[{"id": "inbox", "kind": "file",
                            "folder": str(folder)}])
    assert out["ok"], out


def test_arm_refuses_a_key_only_one_of_two_triggers_supplies(doc, runner,
                                                             tmp_path):
    """A workflow armed on both runs from either, so a key only one carries is a
    run that refuses every time the other fires."""
    folder = tmp_path / "drop"
    folder.mkdir()
    runner.triggerInputs = [{"step": "n1", "label": "Scan", "name": "path",
                             "key": "dir", "kind": "str"}]
    out = wt.arm(doc, tools=["mcp__mail__search_mail"],
                 triggers=[{"id": "inbox", "kind": "file",
                            "folder": str(folder)},
                           {"id": "nightly", "kind": "schedule",
                            "cron": "0 3 * * *"}])
    assert not out["ok"] and out["reason"] == "unsatisfiable_input"


# ---------------------------------------------- 6: the sweep's budget


def test_the_sweep_budget_is_spent_on_arrivals_not_on_known_files(
        doc, runner, drop, monkeypatch):
    """A folder holding more known files than the budget must still see a new
    one — the cap bounds the work an arrival costs, not the work of looking."""
    for n in range(6):
        target = drop / ("old%d.txt" % n)
        target.write_text("x")
        _old(target)
    wt.arm(doc, tools=["mcp__mail__search_mail"],
           triggers=[{"id": "inbox", "kind": "file", "folder": str(drop)}])
    assert len(wt.get(doc)["seen"]) == 6      # all six seeded, none fired
    assert not runner.starts

    # Now the budget is smaller than the number of files already known. Spending
    # it on the `seen` lookups — which is what the bug did — means the arrival
    # below is never reached on any tick, forever.
    monkeypatch.setattr(wt, "SWEEP_MAX_FILES", 3)

    # Named so it sorts LAST. The sweep walks in name order, so this is the
    # arrival a budget spent on known files provably never reaches — with the
    # bug it is not delayed by a tick, it is never seen at all.
    fresh = drop / "zz-new.txt"
    fresh.write_text("x")
    _old(fresh)
    started = wt.tick()
    assert len(started) == 1
    assert started[0]["payload"]["name"] == "zz-new.txt"
    # …and a second tick does not re-fire it.
    runner.finish = {"done": True}
    wt.tick()
    assert len(runner.starts) == 1


# ------------------------------------------ 8: zero bytes is not an arrival


def test_an_empty_file_never_triggers_however_old(doc, runner, drop):
    wt.arm(doc, tools=["mcp__mail__search_mail"],
           triggers=[{"id": "inbox", "kind": "file", "folder": str(drop)}])
    target = drop / "stalled.bin"
    target.write_text("")
    _old(target, 3600)
    assert wt.tick() == []
    assert wt.get(doc)["seen"] == {}

    # …and fires the moment there are bytes in it.
    target.write_text("data")
    _old(target)
    assert len(wt.tick()) == 1


# ------------------------------------------ 7: one job row per workflow


def test_two_workflows_over_the_same_tools_get_different_job_rows(
        doc, runner, tmp_path):
    other = tmp_path / "other.workflow.json"
    other.write_text("{}")
    a = {"path": doc, "fingerprint": "sha256:same"}
    b = {"path": str(other), "fingerprint": "sha256:same"}
    assert wt._job_id(a) != wt._job_id(b)
    assert wt._job_id(a).startswith(wt._JOB_PREFIX)


# ------------------------------------------ 10: an idle tick writes nothing


def test_an_idle_tick_does_not_rewrite_the_store(doc, runner, monkeypatch):
    wt.arm(doc, tools=["mcp__mail__search_mail"],
           triggers=[{"id": "yearly", "kind": "schedule", "cron": "0 3 1 1 *"}])
    writes = []
    real_write = wt._write
    monkeypatch.setattr(wt, "_write", lambda flows: (writes.append(1),
                                                     real_write(flows))[1])
    wt.tick()
    wt.tick()
    assert writes == []


def test_a_tick_that_changes_something_does_write(doc, runner, monkeypatch):
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1})
    writes = []
    real_write = wt._write
    monkeypatch.setattr(wt, "_write", lambda flows: (writes.append(1),
                                                     real_write(flows))[1])
    wt.tick()
    assert writes


def test_a_disarm_during_an_unanswered_start_still_cancels(doc, runner):
    """Revocation wins over uncertainty: the session may be away, and the cost
    of cancelling one that never existed is a refusal nobody reads."""
    wt.arm(doc, tools=["mcp__mail__search_mail"], triggers=_cron_every_minute())
    wt.enqueue(doc, {"a": 1})

    def both_happen_now():
        wt.disarm(doc)
        runner.start_raises = True

    runner.on_start = both_happen_now
    assert wt.tick() == []
    wf = wt.get(doc)
    assert wf["current"] is None
    assert wf["runs"][-1]["state"] == "cancelled"
    assert wf["consecutive_errors"] == 0
    # The cancel was attempted against the id core chose, whatever became of the
    # start call.
    assert runner.cancels == [wf["runs"][-1]["runId"]]
