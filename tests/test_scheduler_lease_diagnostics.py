"""The scheduler names itself and keeps a heartbeat in the machine-duties
lease file, and a process parked behind it says in the log when that holder
has stopped ticking while due tasks wait (2026-10-31: a New task form entry
sat in Upcoming for ever, chat kept working, and no log could say which
process was supposed to send it)."""
import json
import logging
import os
import re
import subprocess
import sys
import threading

import pytest

from fused_render import queue_manager as qm
from fused_render import schedule, tasks_store
from tests import _lease_rival

LEASE = tasks_store.DUTIES_LEASE
# Captured at import, before conftest's autouse fixture swaps the real
# function for a no-op in every test (see tests/test_queue_manager.py).
_REAL_ENSURE_DUTIES_WAITER = qm.ensure_duties_waiter


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    from fused_render import _branch

    d = tmp_path / "claude-sessions"
    d.mkdir()
    monkeypatch.setattr(tasks_store, "STATE_DIR", str(d))
    # Baseline, whatever shell this runs in: `duties_dir()` nests on the
    # branch ref, and a dev shell's FUSED_RENDER_BRANCH would move every file
    # below out of `state_dir`.
    monkeypatch.setenv("FUSED_RENDER_BRANCH", "")
    monkeypatch.setattr(_branch, "_CACHED_REF", None)
    tasks_store.reset_leases_for_tests()
    yield d
    tasks_store.reset_leases_for_tests()


@pytest.fixture(scope="module")
def dead_pid() -> int:
    """A pid that existed and is gone: a child spawned and reaped here."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _lease_path(state_dir) -> str:
    return os.path.join(str(state_dir), LEASE)


def _record_path(state_dir) -> str:
    return os.path.join(str(state_dir), LEASE + ".json")


# ------------------------------------------------------------ the record


def test_a_lease_this_process_does_not_hold_is_never_written(state_dir):
    assert tasks_store.write_lease_record(LEASE, beat=1.0) == {}
    assert not os.path.exists(_record_path(state_dir))


def test_winning_the_lease_stamps_who_won_it_beside_the_lock(state_dir):
    """The record is a SIDECAR, not the lock file's content: Windows locks
    byte 0 against every reader but the holder, and a rewrite in place has an
    empty instant. The lock file itself stays untouched (and empty)."""
    tasks_store.acquire_lease_blocking(LEASE)
    assert os.path.getsize(_lease_path(state_dir)) == 0
    record = json.loads(open(_record_path(state_dir), encoding="utf-8").read())
    assert record["pid"] == os.getpid()
    assert record["label"]
    assert record["since"] > 0
    assert not [n for n in os.listdir(str(state_dir)) if n.endswith(".tmp")]
    holder = tasks_store.lease_holder(LEASE)
    assert holder["held_here"] is True
    assert holder["alive"] is True


def test_a_heartbeat_adds_fields_and_keeps_since(state_dir):
    tasks_store.acquire_lease_blocking(LEASE)
    first = tasks_store.read_lease_record(LEASE)
    tasks_store.write_lease_record(LEASE, beat=123.0, sent=2)
    again = tasks_store.read_lease_record(LEASE)
    assert again["since"] == first["since"]
    assert again["pid"] == os.getpid()
    assert (again["beat"], again["sent"]) == (123.0, 2)
    # A rewrite replaces, never appends: the file is one JSON object.
    tasks_store.write_lease_record(LEASE, beat=124.0)
    raw = open(_record_path(state_dir), encoding="utf-8").read()
    assert json.loads(raw)["beat"] == 124.0


def test_a_rival_reads_the_holder_as_not_here_but_alive(state_dir):
    """What the OTHER process sees: the record names a pid that exists, and
    `held_here` is false — the lease is somebody else's."""
    tasks_store.acquire_lease_blocking(LEASE)
    tasks_store.reset_leases_for_tests()  # drop our handle, keep the file
    holder = tasks_store.lease_holder(LEASE)
    assert holder["held_here"] is False
    assert holder["pid"] == os.getpid()
    assert holder["alive"] is True


def test_an_unstamped_or_unreadable_file_reads_as_no_record(state_dir):
    assert tasks_store.lease_holder(LEASE) == {"held_here": False}
    with open(_record_path(state_dir), "w", encoding="utf-8") as fh:
        fh.write("not json")
    assert tasks_store.read_lease_record(LEASE) == {}
    assert tasks_store.lease_holder(LEASE) == {"held_here": False}


def test_pid_alive_answers_for_a_live_and_a_dead_pid(dead_pid):
    assert tasks_store.pid_alive(os.getpid()) is True
    assert tasks_store.pid_alive(str(os.getpid())) is True
    assert tasks_store.pid_alive(dead_pid) is False
    assert tasks_store.pid_alive("junk") is False
    assert tasks_store.pid_alive(None) is False
    # `os.kill(0, 0)` and `os.kill(-1, 0)` succeed; neither names a process.
    assert tasks_store.pid_alive(0) is False
    assert tasks_store.pid_alive(-1) is False
    assert tasks_store.pid_alive(2 ** 40) is False  # OverflowError, not a raise


def test_the_label_names_the_program_and_its_verb_but_no_values(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["/usr/local/bin/fused-render", "open",
                                      "/Users/x/secret dir", "--token", "abc"])
    monkeypatch.setattr(sys, "executable", "/opt/py/bin/python3.12")
    assert tasks_store._process_label() == "python3.12 fused-render open"
    monkeypatch.setattr(sys, "argv", ["/app/fused_render/cli.py", "--no-browser",
                                      "--port", "2641"])
    assert tasks_store._process_label() == "python3.12 cli.py"


# Another live process, as the record's pid: the parent (pytest, or the
# xdist worker's controller) — never this one, which the code reads as "me".
OTHER_PID = os.getppid()


def _holder_file(state_dir, **record):
    with open(_record_path(state_dir), "w", encoding="utf-8") as fh:
        json.dump({"pid": OTHER_PID, "label": "FusedRender old",
                   "version": "0.7.9", **record}, fh)


# ------------------------------------------------------- the scheduler's side


def test_beat_writes_the_heartbeat_only_while_holding(state_dir):
    schedule._beat_start()
    schedule._beat(sent=1, error="", failures=0)
    assert not os.path.exists(_record_path(state_dir))
    tasks_store.acquire_lease_blocking(LEASE)
    schedule._beat_start()
    record = tasks_store.read_lease_record(LEASE)
    assert record["tick_started"] > 0
    assert "beat" not in record
    schedule._beat(sent=3, error="boom", failures=2)
    record = tasks_store.read_lease_record(LEASE)
    assert record["sent"] == 3
    assert record["error"] == "boom"
    assert record["failures"] == 2
    assert record["beat"] >= record["tick_started"]
    assert record["beat_at"].endswith("+00:00")


def test_a_hold_episode_ends_when_the_entry_is_claimed(monkeypatch):
    """`_claim` forgets the entry, so the next hold of the same message is
    said at INFO again rather than waiting out the hourly repeat."""
    monkeypatch.setattr(schedule, "_hold_said", {"e9": 1.0})
    entries = [{"id": "e9", "state": schedule.PENDING, "due": "d"}]
    monkeypatch.setattr(schedule, "_read", lambda: list(entries))
    monkeypatch.setattr(schedule, "_write", lambda es: None)
    monkeypatch.setattr(schedule, "_sync_wake", lambda: None)
    assert schedule._claim("e9", schedule._now()) is not None
    assert "e9" not in schedule._hold_said


def test_a_hold_is_said_once_at_info_then_at_debug(caplog, monkeypatch):
    monkeypatch.setattr(schedule, "_hold_said", {})
    with caplog.at_level(logging.DEBUG, logger="fused_render.schedule"):
        schedule._log_hold("e1", "at the session cap")
        schedule._log_hold("e1", "at the session cap")
        schedule._log_hold("e2", "session s has a live turn")
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert [r.getMessage() for r in infos] == [
        "holding e1: at the session cap",
        "holding e2: session s has a live turn",
    ]
    assert [r.getMessage() for r in debugs] == ["holding e1: at the session cap"]


def test_created_line_names_this_process_when_it_is_the_scheduler(
        state_dir, caplog):
    tasks_store.acquire_lease_blocking(LEASE)
    schedule._beat(sent=0, error="", failures=0)
    with caplog.at_level(logging.INFO, logger="fused_render.schedule"):
        schedule._log_created({"id": "x1", "due": "2026-10-31T10:00:00+00:00",
                               "immediate": True})
    line = caplog.records[-1].getMessage()
    assert "created x1 due 2026-10-31T10:00:00+00:00 (immediate)" in line
    assert f"scheduler = this process (pid {os.getpid()})" in line
    assert re.search(r"last tick \d+s ago$", line)


def test_created_line_names_another_process_a_stale_record_or_nobody(
        state_dir, caplog, dead_pid):
    with caplog.at_level(logging.INFO, logger="fused_render.schedule"):
        schedule._log_created({"id": "x2", "due": "d"})
    assert "scheduler = no scheduler record on this machine, last tick never" in (
        caplog.records[-1].getMessage())
    _holder_file(state_dir, beat=1.0)
    with caplog.at_level(logging.INFO, logger="fused_render.schedule"):
        schedule._log_created({"id": "x3", "due": "d"})
    assert f"scheduler = pid {OTHER_PID} (FusedRender old, v0.7.9)" in (
        caplog.records[-1].getMessage())
    # A dead pid in the record while somebody holds the lock: the holder is
    # an older build that never stamps — say that, not "pid N is GONE".
    _holder_file(state_dir, pid=dead_pid, beat=1.0)
    with caplog.at_level(logging.INFO, logger="fused_render.schedule"):
        schedule._log_created({"id": "x4", "due": "d"})
    line = caplog.records[-1].getMessage()
    assert "scheduler = an unstamped process (older build)" in line
    assert f"last stamped pid {dead_pid} (FusedRender old) is gone" in line


def test_due_pending_count_accepts_epoch_seconds(monkeypatch):
    entries = [
        {"id": "a", "state": "pending", "due": "2026-01-01T00:00:00+00:00"},
        {"id": "b", "state": "pending", "due": "2099-01-01T00:00:00+00:00"},
        {"id": "c", "state": "sent", "due": "2026-01-01T00:00:00+00:00"},
        {"id": "d", "state": "pending", "due": "garbage"},
    ]
    monkeypatch.setattr(schedule, "_read", lambda: entries)
    assert schedule.due_pending_count(1_800_000_000.0) == 1
    assert schedule.due_pending_count() == 1


# ---------------------------------------------------- the parked process's side


def test_watch_is_quiet_while_the_leader_ticks(state_dir, caplog, monkeypatch):
    monkeypatch.setattr(schedule, "due_pending_count", lambda now: 3)
    _holder_file(state_dir, beat=1_000.0)
    with caplog.at_level(logging.INFO, logger="fused_render.queue_manager"):
        assert qm._duties_check(1_010.0, 0.0) == 0.0
    assert caplog.records == []


def test_watch_is_quiet_with_nothing_due(state_dir, caplog, monkeypatch):
    monkeypatch.setattr(schedule, "due_pending_count", lambda now: 0)
    _holder_file(state_dir, beat=0.0)
    with caplog.at_level(logging.INFO, logger="fused_render.queue_manager"):
        assert qm._duties_check(10_000.0, 0.0) == 0.0
    assert caplog.records == []


def test_watch_is_quiet_when_this_process_holds_the_lease(state_dir, caplog,
                                                          monkeypatch):
    monkeypatch.setattr(schedule, "due_pending_count", lambda now: 3)
    tasks_store.acquire_lease_blocking(LEASE)
    with caplog.at_level(logging.INFO, logger="fused_render.queue_manager"):
        assert qm._duties_check(1e12, 0.0) == 0.0
    assert caplog.records == []


def test_watch_warns_about_a_leader_that_stopped_ticking(state_dir, caplog,
                                                        monkeypatch):
    monkeypatch.setattr(schedule, "due_pending_count", lambda now: 2)
    _holder_file(state_dir, beat=1_000.0)
    now = 1_000.0 + qm._LEADER_STALE_S + 1
    with caplog.at_level(logging.WARNING, logger="fused_render.queue_manager"):
        assert qm._duties_check(now, 0.0) == now
    line = caplog.records[-1].getMessage()
    assert (f"scheduler pid {OTHER_PID} (FusedRender old, v0.7.9) has shown no "
            "activity for 181s") in line
    assert "2 due task(s) waiting" in line
    assert "quit it and this one takes over" in line
    # …and not again inside the repeat window.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="fused_render.queue_manager"):
        assert qm._duties_check(now + 10, now) == now
    assert caplog.records == []


def test_a_slow_pass_is_not_a_stopped_scheduler(state_dir, caplog, monkeypatch):
    """`tick_started` is stamped before every pass; a pass that is still
    running (old `beat`, fresh `tick_started`) is activity, not a wedge."""
    monkeypatch.setattr(schedule, "due_pending_count", lambda now: 2)
    _holder_file(state_dir, beat=1_000.0, tick_started=1_900.0)
    with caplog.at_level(logging.WARNING, logger="fused_render.queue_manager"):
        assert qm._duties_check(1_950.0, 0.0) == 0.0
    assert caplog.records == []


def test_watch_names_an_unstamped_holder_when_the_record_is_stale(
        state_dir, caplog, monkeypatch, dead_pid):
    monkeypatch.setattr(schedule, "due_pending_count", lambda now: 1)
    _holder_file(state_dir, pid=dead_pid, beat=1.0)
    with caplog.at_level(logging.WARNING, logger="fused_render.queue_manager"):
        qm._duties_check(5_000.0, 0.0)
    line = caplog.records[-1].getMessage()
    assert "held by a process that did not stamp it (an older build)" in line
    assert f"pid {dead_pid} (FusedRender old, v0.7.9), is gone" in line


def test_watch_warns_about_a_leader_that_never_ticked_or_keeps_failing(
        state_dir, caplog, monkeypatch):
    monkeypatch.setattr(schedule, "due_pending_count", lambda now: 1)
    _holder_file(state_dir)
    with caplog.at_level(logging.WARNING, logger="fused_render.queue_manager"):
        qm._duties_check(5_000.0, 0.0)
    assert "has never started a pass" in caplog.records[-1].getMessage()
    caplog.clear()
    _holder_file(state_dir, beat=5_000.0, error="ValueError: bad store")
    with caplog.at_level(logging.WARNING, logger="fused_render.queue_manager"):
        qm._duties_check(5_001.0, 0.0)
    assert "last pass failed (ValueError: bad store)" in caplog.records[-1].getMessage()


def test_watch_warns_when_nobody_has_written_a_record(state_dir, caplog,
                                                     monkeypatch):
    monkeypatch.setattr(schedule, "due_pending_count", lambda now: 1)
    with caplog.at_level(logging.WARNING, logger="fused_render.queue_manager"):
        qm._duties_check(5_000.0, 0.0)
    assert "no scheduler record on this machine" in caplog.records[-1].getMessage()


def test_waiter_logs_who_it_parks_behind_and_when_it_wins(state_dir, caplog,
                                                          monkeypatch):
    """The real waiter thread, behind a rival open file description that
    stands in for another process: it names the holder before parking and
    reports the takeover once the rival lets go."""
    from fused_render import project_queue

    monkeypatch.setattr(schedule, "start", lambda: None)
    monkeypatch.setattr(project_queue, "enabled", lambda: False)
    qm.reset_for_tests(None)
    _holder_file(state_dir, beat=1.0)  # the rival's record, then its lock
    rival = _lease_rival.hold(_lease_path(state_dir))
    try:
        with caplog.at_level(logging.INFO, logger="fused_render.queue_manager"):
            thread = _REAL_ENSURE_DUTIES_WAITER()
            assert not thread.join(timeout=0.3) and thread.is_alive()
            _lease_rival.release(rival)
            thread.join(timeout=5)
        assert not thread.is_alive()
    finally:
        try:
            _lease_rival.release(rival)
        except Exception:  # noqa: BLE001 — already released above
            pass
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith(f"machine duties: parked behind pid {OTHER_PID} "
                            "(FusedRender old, v0.7.9)") for m in messages), messages
    assert any(m.startswith(f"machine duties: pid {os.getpid()} now runs the scheduler")
               for m in messages), messages
    # The winner rewrote the file as itself.
    assert tasks_store.lease_holder(LEASE)["held_here"] is True
    assert isinstance(qm._duties_watch_thread, threading.Thread)


# ------------------------------------------------------- the diagnostics zip


def test_diagnostics_bundle_section_names_the_holder_and_summarizes_the_store(
        state_dir, monkeypatch):
    """`state/scheduler.txt` in the diagnostics bundle: the lease record, the
    lock's `lsof`, and the schedule store by counts and pending entries —
    never a message's text (user-authored)."""
    from fused_render import diagnostics
    from fused_render.shell import storage

    home = storage.home_dir()
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "scheduled_messages.json"), "w", encoding="utf-8") as fh:
        json.dump({"entries": [
            {"id": "p1", "state": "pending", "due": "2026-10-31T10:00:00+00:00",
             "message": "SECRET WORDS", "target": "/Users/x/proj", "immediate": True},
            {"id": "s1", "state": "sent", "due": "2026-10-30T10:00:00+00:00",
             "message": "MORE SECRET WORDS"},
        ]}, fh)
    tasks_store.acquire_lease_blocking(LEASE)
    schedule._beat(sent=0, error="", failures=0)
    text = diagnostics._scheduler_text()
    assert f'"pid": {os.getpid()}' in text
    assert '"held_here": true' in text
    assert re.search(r"beat: \d+s ago", text)
    assert '"by state"' not in text  # one JSON object, not a quoted key
    assert 'by state: {"pending": 1, "sent": 1}' in text
    assert '"id": "p1"' in text and '"immediate": true' in text
    assert "SECRET" not in text
    assert "lsof" in text  # ran, or said it was not found — either way present
