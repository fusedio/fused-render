"""The specialists' timer (PLAN C.3; acceptance A for the scheduler, X for recovery)."""
import os

from _specialists_conftest import *
from _specialists_conftest import make_specialist, needs_git, stub_side

from fused_render.specialists import nightly, paths, reviewer, scheduler


def test_flag_off_never_starts(monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_SPECIALISTS", "0")
    scheduler.start()
    try:
        assert not scheduler.running()
        assert not os.path.exists(paths.root())
        assert scheduler.tick()["skipped"] == "specialists are off"
    finally:
        scheduler.stop(0.5)


def test_start_stop_with_flag_on(monkeypatch):
    monkeypatch.setenv(scheduler.START_DELAY_ENV, "3600")
    scheduler.start()
    scheduler.start()  # idempotent
    try:
        assert scheduler.running()
    finally:
        scheduler.stop(2.0)
    assert not scheduler.running()


def test_tick_runs_reviews_then_one_due_nightly(monkeypatch):
    a = make_specialist("Alpha")
    b = make_specialist("Beta")
    order = []
    monkeypatch.setattr(reviewer, "review_pass", lambda now=None, stop=None: order.append("reviews") or {"reviews": 0})
    monkeypatch.setattr(nightly, "due", lambda sid, now, idle: True)
    monkeypatch.setattr(nightly, "run_job", lambda sid: order.append(sid) or {"status": "done"})
    out = scheduler.tick()
    assert out["ran"] and order[0] == "reviews" and len(order) == 2 and order[1] in (a.id, b.id)


def test_tick_skips_while_another_process_holds_the_lock(monkeypatch):
    make_specialist()
    import subprocess
    import sys

    from fused_render.specialists.counts import file_lock
    lock = os.path.join(paths.state_root(), "scheduler.lock")
    os.makedirs(paths.state_root(), exist_ok=True)
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl,sys,time; f=open(sys.argv[1],'a+'); fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); "
        "time.sleep(30)"), lock], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert scheduler.tick()["skipped"] == "another process holds the scheduler"
    finally:
        holder.kill()
        holder.wait()
    with file_lock(lock, blocking=False) as held:
        assert held


def test_due_rules(monkeypatch):
    s = make_specialist()
    monkeypatch.setattr(nightly, "live", lambda sid: False)
    now = 10_000_000.0
    assert nightly.due(s.id, now, app_idle_for=10_000)
    assert not nightly.due(s.id, now, app_idle_for=10)  # app not idle
    st = nightly.state(s.id)
    st["last_ok"] = now - 3600
    nightly._save_state(s.id, st)
    assert not nightly.due(s.id, now, app_idle_for=10_000)  # ran an hour ago
    st["last_ok"] = now - 25 * 3600
    nightly._save_state(s.id, st)
    assert nightly.due(s.id, now, app_idle_for=10_000)
    monkeypatch.setattr(nightly, "live", lambda sid: True)
    assert not nightly.due(s.id, now, app_idle_for=10_000)  # a chat turn is live in the home


@needs_git
def test_start_recovers_a_cut_short_nightly(monkeypatch):
    s = make_specialist()
    st = nightly.state(s.id)
    st["active"] = {"run": "20261118-030000-abcd", "started": 1.0, "stage": "running", "before": "", "notes": [],
                    "cursors": {}}
    nightly._save_state(s.id, st)
    with open(os.path.join(s.home, "memory.md"), "a") as fh:
        fh.write("- [m-eeee] partial {basis=guess; seen=1}\n")
    calls = stub_side(monkeypatch)
    scheduler.recover()
    assert "active" not in nightly.state(s.id) and calls == []
    from fused_render.specialists import changes
    rec = changes.get(s.id, "20261118-030000-abcd")
    assert rec["status"] == "interrupted" and rec["items"][0]["file"] == "memory.md"
