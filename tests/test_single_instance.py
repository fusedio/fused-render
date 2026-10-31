"""single_instance.py — one desktop app per machine, decided at startup by a
flock held for life. The story is in the module doc: a one-second probe at
the wrong moment started a second full copy of the app, which later held the
scheduler lease for a day."""
import json
import logging
import os
import sys

import pytest

from fused_render import single_instance as si

APP = "/Applications/FusedRender.app/Contents/MacOS/FusedRender"
# The real-lock tests need a second flock holder (tests/_lease_rival, fcntl);
# imported inside them so the module still collects on Windows.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="flock rival needs fcntl")


def _rival():
    from tests import _lease_rival

    return _lease_rival


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture()
def support(tmp_path):
    si.release_for_tests()
    yield str(tmp_path)
    si.release_for_tests()


def _probe(alive_pids):
    """A `probe_pid` stand-in: raise for anything not in `alive_pids`."""
    def probe(pid):
        if pid not in alive_pids:
            raise ProcessLookupError(pid)
    return probe


# ------------------------------------------------------------------ the lock


@posix_only
def test_claim_is_exclusive_across_open_file_descriptions_and_idempotent(support):
    assert si.claim(support) is True
    assert si.claim(support) is True  # already ours
    with pytest.raises(OSError):
        _rival().hold(si.lock_path(support))  # somebody else cannot
    record = json.load(open(si.record_path(support), encoding="utf-8"))
    assert record["pid"] == os.getpid()


@posix_only
def test_claim_fails_while_a_rival_holds_the_lock_and_wins_after(support):
    rival = _rival().hold(si.lock_path(support))
    try:
        assert si.claim(support) is False
    finally:
        _rival().release(rival)
    assert si.claim(support) is True


def test_holder_names_the_recorded_pid_while_it_lives_whatever_its_binary(support):
    """The lock decides; the holder's executable is never asked — a copy run
    from the DMG or a translocated bundle holds the lock all the same."""
    assert si.holder(support) is None  # no record
    with open(si.record_path(support), "w", encoding="utf-8") as fh:
        json.dump({"pid": 4242}, fh)
    assert si.holder(support, alive=lambda pid: False) is None  # dead
    assert si.holder(support, alive=lambda pid: True) == 4242
    with open(si.record_path(support), "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid()}, fh)
    assert si.holder(support, alive=lambda pid: True) is None  # ourselves: not a holder to wait on
    with open(si.record_path(support), "w", encoding="utf-8") as fh:
        fh.write("junk")
    assert si.holder(support, alive=lambda pid: True) is None


@posix_only
def test_a_pid_holding_a_lock_in_another_branch_dir_is_another_app(tmp_path, support):
    """Same binary, launched under another FUSED_RENDER_BRANCH: its lock lives
    in `branches/<ref>/`. It holds that lock, so it is not a leftover."""
    base = tmp_path / "base"
    other = base / "branches" / "qa"
    other.mkdir(parents=True)
    assert si.lock_domains(str(base)) == [str(base), str(other)]
    # Our own process holds a real lock in `other` (stands in for the QA app).
    assert si.claim(str(other)) is True
    assert si.holds_a_lock(os.getpid(), str(base)) is True
    # A record naming a pid whose lock nobody holds is just a stale record.
    si.release_for_tests()
    assert si.holds_a_lock(os.getpid(), str(base)) is False
    assert si.holds_a_lock(os.getpid(), None) is False
    assert si.holds_a_lock(os.getpid(), str(tmp_path / "missing")) is False


def test_holder_since_reads_the_record_or_nothing(support):
    assert si.holder_since(support) is None
    with open(si.record_path(support), "w", encoding="utf-8") as fh:
        json.dump({"pid": 1, "since": 1700.5}, fh)
    assert si.holder_since(support) == 1700.5


# ------------------------------------------------------------- ps parsing


def test_parse_etime_reads_every_ps_shape_and_unknown_is_zero_not_ancient():
    assert si.parse_etime("12:34") == 12 * 60 + 34
    assert si.parse_etime("01:02:03") == 3723
    assert si.parse_etime("2-01:02:03") == 2 * 86400 + 3723
    assert si.parse_etime("junk") == 0.0
    assert si.parse_etime("05") == 0.0


def test_list_processes_parses_ps_and_survives_failure():
    class R:
        stdout = ("  1   0 12:34 Ss /sbin/launchd\n"
                  "4242 501 1-00:00:10 S  /Applications/FusedRender.app/Contents/MacOS/FusedRender\n"
                  "bad line\n")

    rows = si.list_processes(run=lambda *a, **k: R())
    assert rows == [(1, 0, 754.0, "Ss", "/sbin/launchd"),
                    (4242, 501, 86410.0, "S", APP)]

    def boom(*a, **k):
        raise OSError("no ps")

    assert si.list_processes(run=boom) == []


# ------------------------------------------------------------------ rivals


def test_rivals_are_other_live_processes_of_our_user_running_our_binary():
    processes = [
        (100, 501, 50.0, "S", APP),                     # ourselves
        (200, 501, 90_000.0, "S", APP),                 # a leftover
        (201, 501, 10.0, "S", APP),                     # a young one: still a rival (the lock decides)
        (202, 501, 500.0, "Z", APP),                    # a zombie: not a process
        (203, 502, 500.0, "S", APP),                    # another user's app
        (204, 501, 500.0, "S", APP),                    # leads OUR process group
        (300, 501, 500.0, "S", "/Applications/FusedRender.app/Contents/MacOS/python"),  # worker
        (400, 501, 500.0, "S", "/Users/x/other/FusedRender.app/Contents/MacOS/FusedRender"),
        (500, 501, 500.0, "S", "/opt/py/bin/python3.12"),  # a dev server
        (600, 501, 500.0, "S", "FusedRender"),           # Linux-style short comm, ours
        (700, 501, 500.0, "S", APP),                     # ours, but holds its own branch lock
    ]
    exe = {200: APP, 201: APP, 202: APP, 203: APP, 204: APP, 600: APP, 700: APP,
           400: "/Users/x/other/FusedRender.app/Contents/MacOS/FusedRender"}
    found = si.rivals(own_pid=100, own_uid=501, own_pgid=204, own_exe=APP, processes=processes,
                      executable_of=lambda pid: exe.get(pid), support_base="/base",
                      holds_lock=lambda pid, base: pid == 700)
    assert [(r.pid, r.age_s) for r in found] == [(200, 90_000.0), (201, 10.0), (600, 500.0)]


def test_rivals_never_target_an_interpreter_and_only_sweep_when_packaged(monkeypatch):
    """Run from source "our executable" is python itself; matching on it
    would sweep every dev server, test worker and Claude session host on the
    same interpreter. Not packaged → no rivals; a python own_exe → none."""
    rows = [(os.getpid(), 501, 1.0, "S", sys.executable), (2, 501, 99.0, "S", sys.executable)]
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert si.rivals(own_pid=os.getpid(), own_uid=501, own_pgid=-1, processes=rows,
                     executable_of=lambda pid: sys.executable) == []
    # Even told explicitly, an interpreter is never "our binary" to sweep for.
    assert si.rivals(own_pid=os.getpid(), own_uid=501, own_pgid=-1, own_exe=sys.executable,
                     processes=rows, executable_of=lambda pid: sys.executable) == []
    assert si.rivals(own_pid=1, own_uid=501, own_pgid=-1, own_exe="/opt/py/bin/python3.12",
                     processes=[(2, 501, 9.0, "S", "/opt/py/bin/python3.12")],
                     executable_of=lambda pid: "/opt/py/bin/python3.12") == []


def test_rivals_is_empty_without_an_own_executable():
    assert si.rivals(own_pid=1, own_uid=0, own_pgid=-1, own_exe="",
                     processes=[(2, 0, 1.0, "S", APP)], executable_of=lambda pid: APP) == []


def test_own_executable_prefers_the_ps_row_for_our_own_pid():
    rows = [(os.getpid(), 501, 1.0, "S", "/Apps/X.app/Contents/MacOS/X"), (1, 0, 1.0, "S", "/sbin/launchd")]
    assert si.own_executable(rows) == "/Apps/X.app/Contents/MacOS/X"
    exe = si.own_executable(None)
    assert exe and os.path.isabs(exe)


def test_alive_handles_live_dead_and_garbage(monkeypatch):
    assert si.alive(os.getpid()) is True
    monkeypatch.setattr(si._platform, "probe_pid", _probe({os.getpid()}))
    assert si.alive(4242) is False
    assert si.alive(2 ** 40) is False


# ------------------------------------------------------------------- sweep


class World:
    """Processes that die on TERM (or only on KILL, or never)."""

    def __init__(self, on_term=(), on_kill=(), immortal=()):
        self.alive = set(on_term) | set(on_kill) | set(immortal)
        self.on_term, self.on_kill = set(on_term), set(on_kill)
        self.termed, self.killed = [], []

    def is_alive(self, pid):
        return pid in self.alive

    def term(self, pid):
        self.termed.append(pid)
        if pid in self.on_term:
            self.alive.discard(pid)

    def kill(self, pid):
        self.killed.append(pid)
        if pid in self.on_kill:
            self.alive.discard(pid)


def _sweep(world, found):
    clock = Clock()
    return si.sweep(found, alive=world.is_alive, term=world.term, kill=world.kill,
                    clock=clock, sleep=clock.sleep)


def test_sweep_terms_then_kills_and_reports_what_survived():
    world = World(on_term=[10], on_kill=[20], immortal=[30])
    result = _sweep(world, [si.Rival(10, 500.0), si.Rival(20, 500.0), si.Rival(30, 500.0)])
    assert world.termed == [10, 20, 30]
    assert sorted(world.killed) == [20, 30]  # 10 went on TERM, no KILL for it
    assert sorted(result.killed) == [10, 20]
    assert result.failed == [30]


def test_sweep_with_nothing_to_do_touches_nothing():
    world = World()
    assert _sweep(world, []) == si.Sweep()
    assert world.termed == []


# ----------------------------------------------------------------- enforce


def _enforce(support, *, claim, holder=lambda d: None, pidfile=lambda: (None, None),
             alive=lambda p: True, probe=lambda port: False, rivals=lambda: [],
             sweep=lambda rv: si.Sweep(killed=[r.pid for r in rv]), record=None,
             boot_wait_s=10.0, min_wait_s=2.0, since=lambda d: None, wall=lambda: 1000.0,
             is_ours=lambda pid: False):
    clock = Clock()
    out = si.enforce(support, pidfile, claim=claim, holder=holder, holder_since=since,
                     is_ours=is_ours, rivals_of=rivals, sweep_with=sweep, probe=probe,
                     alive=alive, boot_wait_s=boot_wait_s, min_wait_s=min_wait_s,
                     poll_s=0.5, clock=clock, wall=wall, sleep=clock.sleep, record=record)
    return out, clock


def test_the_lock_winner_is_primary_and_sweeps_every_leftover(support, caplog):
    recorded = []
    found = [si.Rival(7, 1000.0), si.Rival(8, 3.0)]  # age is not consulted any more
    with caplog.at_level(logging.WARNING, logger="fused_render.single_instance"):
        out, clock = _enforce(support, claim=lambda d: True, rivals=lambda: found,
                              record=lambda kind, **f: recorded.append((kind, f)))
    assert out.role == "primary"
    assert out.sweep.killed == [7, 8]
    assert clock.t == 0.0
    assert recorded == [("stale-app-copies", {"killed": [7, 8], "failed": []})]
    assert "without the instance lock" in caplog.records[-1].getMessage()


def test_primary_with_no_leftovers_says_nothing(support, caplog):
    with caplog.at_level(logging.INFO, logger="fused_render.single_instance"):
        out, _ = _enforce(support, claim=lambda d: True)
    assert out.role == "primary" and out.sweep == si.Sweep() and caplog.records == []


def test_a_record_that_raises_never_blocks_the_launch(support):
    def boom(kind, **f):
        raise OSError("disk")

    out, _ = _enforce(support, claim=lambda d: True, rivals=lambda: [si.Rival(7, 1.0)],
                      record=boom)
    assert out.sweep.killed == [7]


def test_the_loser_waits_for_the_holder_to_serve_then_defers(support):
    """The field case, fixed: the real app is booting (lock held, no pidfile
    yet, then a pidfile but `/` slow). The newcomer waits instead of starting
    a second copy."""
    pidfiles = iter([(None, None)] * 3 + [(42, 1777)] * 10)
    probes = iter([False] * 2 + [True])
    out, clock = _enforce(support, claim=lambda d: False, holder=lambda d: 42,
                          pidfile=lambda: next(pidfiles), probe=lambda port: next(probes))
    assert out.role == "duplicate"
    assert out.live == (42, 1777) and out.holder == 42
    assert 2.0 <= clock.t <= 3.0


def test_the_loser_becomes_primary_when_the_holder_exits_meanwhile(support):
    # Poll 1: lock held, holder 42 alive. Poll 2: 42 gone → claim wins.
    world = {"holder_alive": True, "lock_free": False}

    def claim(d):
        return world["lock_free"]

    def alive(pid):
        if not world["holder_alive"]:
            world["lock_free"] = True
            return False
        world["holder_alive"] = False  # dies after this look
        return True

    out, clock = _enforce(support, claim=claim, holder=lambda d: 42, alive=alive,
                          rivals=lambda: [si.Rival(9, 1.0)])
    assert out.role == "primary"
    assert out.sweep.killed == [9]
    assert out.waited_s > 0


def test_a_holder_that_never_serves_is_reported_after_the_budget(support):
    out, clock = _enforce(support, claim=lambda d: False, holder=lambda d: 42, boot_wait_s=5.0)
    assert out.role == "duplicate" and out.live is None and out.holder == 42
    assert clock.t >= 5.0


def test_the_wait_counts_from_the_holders_own_start(support):
    """A holder that began booting 70 s ago gets 5 more seconds of a 75 s
    budget, never less than the floor; one that began 1 s ago gets ~74."""
    out, clock = _enforce(support, claim=lambda d: False, holder=lambda d: 42,
                          since=lambda d: 1000.0 - 70.0, wall=lambda: 1000.0,
                          boot_wait_s=75.0, min_wait_s=2.0)
    assert out.live is None and 5.0 <= clock.t < 6.0
    out, clock = _enforce(support, claim=lambda d: False, holder=lambda d: 42,
                          since=lambda d: 1000.0 - 200.0, wall=lambda: 1000.0,
                          boot_wait_s=75.0, min_wait_s=2.0)
    assert 2.0 <= clock.t < 3.0  # the floor
    out, clock = _enforce(support, claim=lambda d: False, holder=lambda d: 42,
                          since=lambda d: 1000.0 - 1.0, wall=lambda: 1000.0,
                          boot_wait_s=75.0, min_wait_s=2.0)
    assert 74.0 <= clock.t < 75.0


def test_the_lock_holder_is_deferred_to_whatever_its_binary(support):
    """Translocated / DMG / renamed copy holding the lock: its pidfile pid is
    the holder's pid, so it is deferred to even though `is_ours` says no."""
    out, clock = _enforce(support, claim=lambda d: False, holder=lambda d: 42,
                          pidfile=lambda: (42, 1777), probe=lambda port: True,
                          is_ours=lambda pid: False)
    assert out.role == "duplicate" and out.live == (42, 1777) and clock.t == 0.0


def test_a_pidfile_naming_a_stranger_is_not_deferred_to(support):
    """The lock's record names 42; a crash left a pidfile for 555, a pid since
    recycled by something else that happens to listen on the port. Not the
    holder and not ours → wait for 42 instead."""
    asked = []

    def is_ours(pid):
        asked.append(pid)
        return False

    out, clock = _enforce(support, claim=lambda d: False, holder=lambda d: 42,
                          pidfile=lambda: (555, 1777), probe=lambda port: True,
                          is_ours=is_ours, boot_wait_s=3.0)
    assert out.role == "duplicate" and out.live is None and clock.t >= 3.0
    assert asked == [555]  # asked once, then cached across polls
    # …but a pidfile pid that IS our binary (two records drifted) is deferred to.
    out, _ = _enforce(support, claim=lambda d: False, holder=lambda d: 42,
                      pidfile=lambda: (555, 1777), probe=lambda port: True,
                      is_ours=lambda pid: True)
    assert out.live == (555, 1777)


def test_unknown_holder_no_pidfile_and_a_held_lock_still_ends_at_the_budget(support):
    """The lock is held by something that wrote no record (an older build),
    there is no pidfile and the claim keeps failing: the loop must still end."""
    out, clock = _enforce(support, claim=lambda d: False, holder=lambda d: None,
                          boot_wait_s=4.0)
    assert out.role == "duplicate" and out.live is None and out.holder is None
    assert 4.0 <= clock.t < 5.0


def test_an_older_build_holder_with_no_record_still_gets_the_wait(support):
    """A holder from before this module wrote no record: `holder` is None,
    the pidfile is the only name, and we still defer to it once it serves."""
    out, _ = _enforce(support, claim=lambda d: False, holder=lambda d: None,
                      pidfile=lambda: (42, 1777), probe=lambda port: True)
    assert out.role == "duplicate" and out.live == (42, 1777) and out.holder is None


@posix_only
def test_enforce_end_to_end_against_a_real_rival_lock(support):
    """A real second open file description holds the lock (stands in for the
    running app); with a serving pidfile the newcomer defers in one poll."""
    rival = _rival().hold(si.lock_path(support))
    try:
        clock = Clock()
        out = si.enforce(support, lambda: (os.getppid(), 1777), probe=lambda port: True,
                         holder=lambda d: None, is_ours=lambda pid: True,
                         clock=clock, sleep=clock.sleep)
        assert out.role == "duplicate" and out.live == (os.getppid(), 1777)
    finally:
        _rival().release(rival)
    out = si.enforce(support, lambda: (None, None), rivals_of=lambda: [],
                     sweep_with=lambda rv: si.Sweep())
    assert out.role == "primary"


# ------------------------------------------------------------ app wiring


def test_app_gate_primary_returns_none_and_duplicate_returns_the_live_app(monkeypatch):
    import fused_render.app as app_mod

    monkeypatch.setattr(si, "enforce", lambda d, r, **k: si.Outcome(role="primary"))
    assert app_mod._enforce_single_instance() is None
    monkeypatch.setattr(si, "enforce",
                        lambda d, r, **k: si.Outcome(role="duplicate", live=(5, 1777), holder=5))
    assert app_mod._enforce_single_instance() == (5, 1777)


def test_app_gate_stalled_holder_asks_rechecks_then_stops_it_or_exits(monkeypatch):
    import fused_render.app as app_mod

    stalled = si.Outcome(role="duplicate", live=None, holder=77, waited_s=75.0)
    swept, leftovers, exits, claims = [], [], [], []
    monkeypatch.setattr(si, "sweep", lambda rv, **k: swept.append([r.pid for r in rv]) or si.Sweep())
    monkeypatch.setattr(si, "sweep_leftovers", lambda **k: leftovers.append(True) or si.Sweep())
    monkeypatch.setattr(si, "claim", lambda d: claims.append(d) or True)
    monkeypatch.setattr(si, "alive", lambda pid: True)
    monkeypatch.setattr(si, "is_our_binary", lambda pid, own=None: True)
    monkeypatch.setattr(app_mod, "hard_exit", lambda code=0, **k: exits.append(code))
    monkeypatch.setattr(si, "enforce", lambda d, r, **k: stalled)
    # "Stop it and start": re-check still stalled → stop 77, take the lock, clear the rest.
    monkeypatch.setattr(app_mod, "_stalled_copy_alert", lambda pid: True)
    assert app_mod._enforce_single_instance() is None
    assert swept == [[77]] and claims and leftovers == [True] and exits == []
    # "Quit": exit, touch nothing.
    swept.clear(); leftovers.clear(); claims.clear()
    monkeypatch.setattr(app_mod, "_stalled_copy_alert", lambda pid: False)
    app_mod._enforce_single_instance()
    assert swept == [] and leftovers == [] and exits == [1]
    # The modal stood a while: the re-check finds a serving app → defer to it.
    exits.clear()
    answers = iter([stalled, si.Outcome(role="duplicate", live=(5, 1777), holder=5)])
    monkeypatch.setattr(si, "enforce", lambda d, r, **k: next(answers))
    monkeypatch.setattr(app_mod, "_stalled_copy_alert", lambda pid: True)
    assert app_mod._enforce_single_instance() == (5, 1777)
    assert swept == []
    # The holder changed under the modal: never sweep a pid we did not ask about.
    answers = iter([stalled, si.Outcome(role="duplicate", live=None, holder=78, waited_s=5.0)])
    monkeypatch.setattr(si, "enforce", lambda d, r, **k: next(answers))
    assert app_mod._enforce_single_instance() is None
    assert swept == [] and leftovers == [True]
    # Consented, stopped, but the lock still cannot be taken (holder stuck in
    # the kernel): start anyway, no exit.
    swept.clear(); leftovers.clear(); exits.clear()
    monkeypatch.setattr(si, "enforce", lambda d, r, **k: stalled)
    monkeypatch.setattr(si, "claim", lambda d: False)
    assert app_mod._enforce_single_instance() is None
    assert swept == [[77]] and leftovers == [] and exits == []


def test_app_gate_falls_back_to_the_pidfile_when_the_lock_dir_is_unusable(monkeypatch):
    import fused_render.app as app_mod

    def boom(d, r, **k):
        raise OSError("read-only volume")

    monkeypatch.setattr(si, "enforce", boom)
    monkeypatch.setattr(app_mod, "read_pidfile", lambda: (None, None))
    assert app_mod._enforce_single_instance() is None
    monkeypatch.setattr(app_mod, "read_pidfile", lambda: (os.getpid(), 1777))
    monkeypatch.setattr(si, "_probe_ok", lambda port: True)
    assert app_mod._enforce_single_instance() == (os.getpid(), 1777)


def test_pidfile_is_removed_only_by_its_owner(monkeypatch, tmp_path):
    import fused_render.app as app_mod

    pidfile, portfile = tmp_path / "server.pid", tmp_path / "server.port"
    monkeypatch.setattr(app_mod, "PIDFILE", str(pidfile))
    monkeypatch.setattr(app_mod, "PORTFILE", str(portfile))
    pidfile.write_text(str(os.getpid() + 1))
    portfile.write_text("1777")
    app_mod._remove_pidfile()
    assert pidfile.exists() and portfile.exists()  # somebody else's
    pidfile.write_text(str(os.getpid()))
    app_mod._remove_pidfile()
    assert not pidfile.exists() and not portfile.exists()
    app_mod._remove_pidfile()  # nothing there: fine
    assert app_mod.read_pidfile() == (None, None)
