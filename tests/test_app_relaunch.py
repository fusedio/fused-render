"""The fused-render://relaunch action (app.py): quit the running app and
respawn it from the bundle on disk, so a newer installed version takes over
without the user hunting for the menu-bar Quit.

All module-level and AppKit-free, like test_app_quit.py: the relauncher is a
detached /bin/sh child that outlives this process (a dying app cannot `open`
its own successor), asserted at the Popen boundary — nothing real is spawned.
"""
import os
import subprocess
import sys

import pytest

import fused_render.app as app_mod


def _fake_executable(tmp_path):
    contents = tmp_path / "FusedRender.app" / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    executable = contents / "MacOS" / "python"
    executable.write_bytes(b"")
    return str(executable)


# --------------------------------------------------------------- bundle_path


def test_bundle_path_is_none_when_unpackaged(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert app_mod.bundle_path() is None


def test_bundle_path_resolves_the_app_root(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(sys, "executable", _fake_executable(tmp_path))
    assert app_mod.bundle_path() == str(tmp_path / "FusedRender.app")


# ---------------------------------------------------------- spawn_relauncher


def test_relauncher_waits_for_this_pid_then_opens_the_bundle():
    calls = []
    app_mod.spawn_relauncher("/Applications/FusedRender.app", 4242,
                             popen=lambda *a, **k: calls.append((a, k)))
    (argv,), kwargs = calls[0]
    assert argv[:2] == ["/bin/sh", "-c"]
    script = argv[2]
    assert "kill -0 4242" in script          # poll THIS pid until it dies
    assert '"$opener" -a "$bundle"' in script
    assert "opener=/usr/bin/open;" in script
    assert "/Applications/FusedRender.app" in script
    # Launch via the launch deep link, not a plain bundle open: a plain open
    # is a normal launch, which boots onto a fresh home tab and steals focus
    # from the page that asked for the restart. The launch action's handler
    # sets state["docs"] and opens nothing (D128).
    assert "fused-render://launch" in script
    # The relauncher must survive its parent's death and hold no pipes to it.
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL


def test_relauncher_quotes_a_bundle_path_with_spaces():
    calls = []
    app_mod.spawn_relauncher("/Applications/My Apps/FusedRender.app", 1,
                             popen=lambda *a, **k: calls.append((a, k)))
    script = calls[0][0][0][2]
    assert "'/Applications/My Apps/FusedRender.app'" in script


# ---- what the relauncher does AFTER our pid dies (real-app test, PR #1214) --
#
# The first cut was `exec open -a <bundle> <url>` the instant `kill -0` failed,
# and it logged nothing at all. On the real app that quit cleanly and NOTHING
# came back: no successor, no relauncher shell left alive, no crash report — and
# no way to tell whether the spawn had even happened. Running the exact same
# `open` by hand twenty seconds later worked first time, which is the signature
# of asking LaunchServices for a new instance while it still believes the dying
# one is the bundle's owner: the URL goes to a process that is already gone and
# `open` reports success.


def test_the_relauncher_settles_before_it_asks_for_a_new_instance():
    calls = []
    app_mod.spawn_relauncher("/Applications/FusedRender.app", 4242,
                             popen=lambda *a, **k: calls.append(a))
    script = calls[0][0][2]
    # The pid wait is unchanged; the sleep AFTER it is the new part.
    assert f"/bin/sleep {app_mod.RELAUNCH_SETTLE_S}" in script
    assert script.index("kill -0 4242") < script.index(
        f"/bin/sleep {app_mod.RELAUNCH_SETTLE_S}")
    assert app_mod.RELAUNCH_SETTLE_S > 0


def test_the_relauncher_verifies_a_successor_instead_of_trusting_open():
    """`open` exits 0 for "I handed the URL to something", which includes handing
    it to the instance that is disappearing — so its exit code says nothing about
    whether the app came back. The successor's pidfile is the signal that does."""
    calls = []
    app_mod.spawn_relauncher("/Applications/FusedRender.app", 1,
                             popen=lambda *a, **k: calls.append(a),
                             pidfile="/tmp/x/server.pid")
    script = calls[0][0][2]
    assert "pidfile=/tmp/x/server.pid;" in script
    assert '[ -f "$pidfile" ]' in script
    # It waits for the file rather than exiting on the `open` return.
    assert "exec /usr/bin/open" not in script


def test_the_relauncher_never_escalates_over_a_live_process():
    """`-n` forces a NEW instance even if one is already running, so it is
    reachable only when `pgrep` says nothing is running out of this bundle. The
    pidfile is a LATE signal — `_bootstrap_server` writes it only after
    `wait_until_ready` — so "no pidfile yet" must never be read as "the launch
    failed" (bugbot, PR #1214)."""
    calls = []
    app_mod.spawn_relauncher("/Applications/FusedRender.app", 1,
                             popen=lambda *a, **k: calls.append(a))
    script = calls[0][0][2]
    assert 'macos="$bundle/Contents/MacOS"' in script
    assert '/usr/bin/pgrep -f "$macos"' in script
    # The shell's own argv carries the bundle path, so it must not match itself.
    assert '/usr/bin/grep -qv "^$$$"' in script
    # A live process means KEEP WAITING; the escalation is the `else` branch.
    assert script.index("if alive; then") < script.index('-n -a "$bundle"')
    assert "still booting, not asking again" in script


def test_the_liveness_probe_is_the_pgrep_line_by_default():
    """Injectable so a test can state "a successor is booting" as a fact instead
    of hoping the host's `pgrep` agrees — but the DEFAULT is what ships, and it
    has to stay the bundle-scoped, self-excluding one."""
    calls = []
    app_mod.spawn_relauncher("/Applications/FusedRender.app", 1,
                             popen=lambda *a, **k: calls.append(a))
    assert f"alive() {{ {app_mod.ALIVE_PROBE}; }}" in calls[0][0][2]
    assert '/usr/bin/pgrep -f "$macos"' in app_mod.ALIVE_PROBE
    # Without the self-exclusion the probe reports "alive" forever — this shell's
    # own command line carries the bundle path — and would never escalate.
    assert '/usr/bin/grep -qv "^$$$"' in app_mod.ALIVE_PROBE

    injected = []
    app_mod.spawn_relauncher("/Applications/FusedRender.app", 1,
                             popen=lambda *a, **k: injected.append(a),
                             alive_probe="false")
    assert "alive() { false; }" in injected[0][0][2]


def test_the_boot_wait_outlasts_the_successors_own_readiness_ceiling():
    """The number that makes the rule above hold even if `pgrep` were useless:
    `_bootstrap_server` waits up to SERVER_READY_TIMEOUT_S for its own server and
    only THEN writes the pidfile, so a healthy launch has none for that long —
    plus the seconds a cold start of a signed bundle takes first."""
    assert app_mod.RELAUNCH_BOOT_WAIT_S > app_mod.SERVER_READY_TIMEOUT_S
    # Not by a hair: the launch itself is seconds before any Python runs.
    assert app_mod.RELAUNCH_BOOT_WAIT_S - app_mod.SERVER_READY_TIMEOUT_S >= 5


# The three tests below RUN the generated shell rather than reading it, and are
# therefore darwin-only — the relauncher exists to drive LaunchServices
# (`open -a`), there is no /bin/sh on the Windows runner at all, and `pgrep -f`'s
# matching differs on Linux. Everything ABOVE this line is a string assertion on
# the script and runs everywhere, which is where the rules themselves are
# pinned; these add "and the shell really does that".
mac_only = pytest.mark.skipif(sys.platform != "darwin",
                              reason="the relauncher drives macOS LaunchServices")


def _run_relauncher(tmp_path, *, alive_probe=None, successor_after=None,
                    wait_s=6.0, real_successor=False):
    """Run the REAL generated shell against a throwaway pid and a stub opener,
    with the clock scaled down so its own logic — not a twenty-five-second sleep
    — is what the test exercises. Returns (opens, log lines)."""
    import subprocess
    import time

    opener = tmp_path / "opener.sh"
    opens = tmp_path / "opens.txt"
    opener.write_text(f'#!/bin/sh\necho "$*" >> {opens}\nexit 0\n')
    opener.chmod(0o755)
    log = tmp_path / "relaunch.log"
    pidfile = tmp_path / "server.pid"
    bundle = str(tmp_path / "Nope.app")

    victim = subprocess.Popen(["/bin/sh", "-c", "sleep 0.3"])
    app_mod.spawn_relauncher(bundle, victim.pid, log=str(log),
                             pidfile=str(pidfile), opener=str(opener),
                             alive_probe=alive_probe)
    victim.wait()
    successor = None
    if successor_after is not None:
        if real_successor:
            # A process whose argv sits under the bundle's MacOS dir — what the
            # DEFAULT probe looks for, and true from the instant a real
            # successor execs.
            successor = subprocess.Popen(
                ["/bin/sh", "-c",
                 f"exec -a {bundle}/Contents/MacOS/FusedRender sleep {wait_s + 2}"])
        else:
            # The injected probe's own signal: a file, so "is a successor
            # booting?" is a fact the test states rather than one it hopes the
            # host's `pgrep` will agree with.
            (tmp_path / "booting").write_text("1")
        time.sleep(successor_after)
        pidfile.write_text("999")
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if log.exists() and ("successor is up" in log.read_text()
                             or "giving up" in log.read_text()):
            break
        time.sleep(0.1)
    if successor is not None:
        successor.terminate()
        successor.wait(timeout=5)
    return (opens.read_text().splitlines() if opens.exists() else [],
            log.read_text().splitlines() if log.exists() else [])


def _fast_clock(monkeypatch, *, boot_wait, deadline):
    monkeypatch.setattr(app_mod, "RELAUNCH_POLL_S", 0.05)
    monkeypatch.setattr(app_mod, "RELAUNCH_SETTLE_S", 0.1)
    monkeypatch.setattr(app_mod, "RELAUNCH_BOOT_WAIT_S", boot_wait)
    monkeypatch.setattr(app_mod, "RELAUNCH_DEADLINE_S", deadline)


@mac_only
def test_a_slow_but_successful_launch_is_asked_for_exactly_once(monkeypatch, tmp_path):
    """THE REGRESSION (bugbot, PR #1214). A successor that takes longer than the
    boot wait to write its pidfile is still a successor: it is alive the whole
    time, and asking again would fork a second copy onto the port the first is
    about to claim.

    The liveness probe is INJECTED — a file the test creates — so what is pinned
    is the RULE ("never `-n` while something is alive") rather than whether this
    host's `pgrep` agrees about a fake process. The default probe gets its own
    test below."""
    _fast_clock(monkeypatch, boot_wait=1.0, deadline=6.0)
    # Pidfile at 3x the boot wait — the successor sails past several boundaries
    # at which the old code would have asked again.
    opens, log = _run_relauncher(
        tmp_path, alive_probe=f'[ -f {tmp_path / "booting"} ]',
        successor_after=3.2, wait_s=8.0)

    assert len(opens) == 1, opens
    assert opens[0].startswith("-a "), opens
    assert not any("-n" in line for line in opens), opens
    joined = "\n".join(log)
    assert "still booting, not asking again" in joined
    assert "successor is up after attempt 1" in joined


@mac_only
def test_a_launch_that_left_nothing_running_does_escalate(monkeypatch, tmp_path):
    """The other half: with the probe finding nothing at all, the first ask
    demonstrably did nothing and `-n` is the right next move."""
    _fast_clock(monkeypatch, boot_wait=1.0, deadline=3.5)
    opens, log = _run_relauncher(tmp_path, alive_probe="false", wait_s=8.0)

    assert len(opens) == app_mod.RELAUNCH_OPEN_TRIES, opens
    assert opens[0].startswith("-a ")
    assert all(line.startswith("-n -a ") for line in opens[1:]), opens
    assert "giving up: the app did not come back" in "\n".join(log)


@mac_only
def test_the_default_probe_sees_a_real_child_of_the_bundle(monkeypatch, tmp_path):
    """The production `pgrep` line itself, against a real process running out of
    the bundle path — the half the injected probe above deliberately does not
    exercise. Darwin-only because that is the only place it has to work, and the
    only place `pgrep -f`'s matching is the one this was written against."""
    _fast_clock(monkeypatch, boot_wait=1.0, deadline=6.0)
    opens, log = _run_relauncher(tmp_path, successor_after=3.2, wait_s=8.0,
                                 real_successor=True)

    assert len(opens) == 1, opens
    assert not any("-n" in line for line in opens), opens
    assert "still booting, not asking again" in "\n".join(log)


def test_the_relauncher_writes_its_own_timeline():
    """A relauncher that logs nowhere is exactly what made "the app never came
    back" undiagnosable: the process that would have written those lines into the
    app log is the one that just exited."""
    calls = []
    app_mod.spawn_relauncher("/Applications/FusedRender.app", 77896,
                             popen=lambda *a, **k: calls.append(a),
                             log="/tmp/logs/fused-render-relaunch-77896.log")
    script = calls[0][0][2]
    assert "log=/tmp/logs/fused-render-relaunch-77896.log;" in script
    assert '>>"$log"' in script
    for line in ("parked on pid 77896", "has exited", "open attempt",
                 "successor is up", "giving up"):
        assert line in script, line


def test_the_relauncher_log_sits_beside_the_app_log(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSED_RENDER_LOG_DIR", str(tmp_path))
    path = app_mod.relauncher_log_path(4242)
    assert os.path.dirname(path) == str(tmp_path)
    # Named for the pid it watches, so `ls -t` puts it next to that process's own
    # `fused-render-<pid>.log` — the one whose last line is the teardown.
    assert path.endswith("fused-render-relaunch-4242.log")


def test_the_spawn_says_so_in_the_app_log(monkeypatch, tmp_path, caplog):
    """One line naming the child's pid, the bundle and the log file — the
    difference between a guess and a diagnosis."""
    monkeypatch.setenv("FUSED_RENDER_LOG_DIR", str(tmp_path))

    class FakeChild:
        pid = 5150

    monkeypatch.setattr(app_mod, "spawn_relauncher",
                        lambda bundle, pid, **kw: FakeChild())
    with caplog.at_level("INFO", logger="fused_render"):
        app_mod._spawn_relauncher_logged("/Applications/FusedRender.app", 4242)
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "5150" in line and "4242" in line
    assert "/Applications/FusedRender.app" in line
    assert "fused-render-relaunch-4242.log" in line


def test_the_pidfile_is_gone_before_the_relauncher_is_spawned():
    """The verification above only means anything because of this ordering: the
    file existing again can only be a NEW instance if the dying one removed its
    own first. `begin_quit` does that inside the claim, ahead of `on_claim`."""
    order = []
    state = {}
    app_mod.begin_quit(
        state,
        start=lambda *a, **k: order.append("teardown"),
        remove_pidfile=lambda: order.append("pidfile gone"),
        on_claim=lambda: order.append("relauncher parked"),
    )
    assert order == ["pidfile gone", "relauncher parked", "teardown"]


# ------------------------------------------------------------ begin_relaunch


def test_begin_relaunch_spawns_inside_the_claimed_quit():
    order = []

    def quit_action(on_claim=None):
        order.append("quit")
        on_claim()          # begin_quit runs this inside the claim
        order.append("teardown-started")
        return True         # claimed the teardown

    started = app_mod.begin_relaunch(
        quit_action=quit_action,
        bundle="/Applications/FusedRender.app",
        spawn=lambda bundle, pid: order.append(("spawn", bundle, pid)),
        running="0.4.8", installed="0.5.0",
    )
    assert started is True
    # Quit first: begin_quit's claim is the atomic (locked) arbiter of whether a
    # teardown is already in flight, so spawning only on a claimed quit is what
    # makes quit-vs-relaunch race-free. But the spawn hangs off the CLAIM rather
    # than off begin_relaunch's own statement order — see
    # test_the_relauncher_is_parked_before_anything_can_terminate.
    assert order == ["quit",
                     ("spawn", "/Applications/FusedRender.app", os.getpid()),
                     "teardown-started"]


def test_the_relauncher_is_parked_before_anything_can_terminate(monkeypatch):
    """The regression D357 could have introduced, pinned deterministically.

    Before the hard exit, death was `AppHelper.callAfter(rumps.quit_application)`
    — queued onto the main run loop, so it could not possibly run until
    `application_openURLs_` had returned, and the spawn preceded it by
    construction. `os._exit` off the watchdog thread has no such hop: an instance
    with no mounts and a server that drains on its first poll can complete the
    whole teardown while the main thread is still inside `Popen` (which
    `start_new_session=True` makes a fork+exec of a large process). The failure
    is invisible — the app quits and nothing comes back — so this test makes the
    race deterministic instead of unlikely: the teardown terminates the INSTANT
    it is started."""
    order = []
    exits = []
    monkeypatch.setattr(app_mod, "hard_exit", lambda code=0: exits.append(code))

    def _start(server, *, terminate, server_thread=None, **kw):
        order.append("teardown")
        terminate()  # nothing to unmount: done before begin_quit even returns

    quit_action = app_mod.make_quit_action(
        {}, terminate=lambda: order.append("exit") or app_mod.hard_exit(),
        start=_start, remove_pidfile=lambda: None)

    started = app_mod.begin_relaunch(
        quit_action=quit_action, bundle="/Applications/FusedRender.app",
        spawn=lambda bundle, pid: order.append("spawn"),
        running="0.4.8", installed="0.5.0",
    )

    assert started is True
    assert exits == [0]
    assert order.index("spawn") < order.index("exit"), (
        "the successor must be parked before this process can die: " + str(order))


def test_begin_relaunch_without_a_bundle_does_nothing(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    calls = []
    started = app_mod.begin_relaunch(
        quit_action=lambda on_claim=None: calls.append("quit") or True,
        spawn=lambda *a: calls.append(a),
        running="0.4.8", installed="0.5.0",
    )
    assert started is False
    assert calls == []  # unpackaged: no respawn possible, so no quit either


def test_begin_relaunch_joins_a_quit_already_in_flight():
    calls = []
    started = app_mod.begin_relaunch(
        quit_action=lambda on_claim=None: False,  # begin_quit joined one in flight
        bundle="/Applications/FusedRender.app",
        spawn=lambda *a: calls.append(a),
        running="0.4.8", installed="0.5.0",
    )
    assert started is False
    assert calls == []  # never respawn an app the user is quitting


def test_begin_relaunch_is_a_noop_when_already_running_the_disk_version():
    # The OS may LAUNCH a fresh instance just to deliver the relaunch link
    # (app not running, or a second click landing after the old pid died).
    # That instance IS the disk version — quitting it to boot itself again
    # would be a pointless extra cycle, so relaunch only acts when stale.
    calls = []
    started = app_mod.begin_relaunch(
        quit_action=lambda on_claim=None: calls.append("quit") or True,
        bundle="/Applications/FusedRender.app",
        spawn=lambda *a: calls.append(a),
        running="0.5.0", installed="0.5.0",
    )
    assert started is False
    assert calls == []


def test_begin_relaunch_is_a_noop_when_the_disk_version_is_unknown():
    # Unreadable Info.plist: staleness can't be established, so don't quit.
    calls = []
    started = app_mod.begin_relaunch(
        quit_action=lambda on_claim=None: calls.append("quit") or True,
        bundle="/Applications/FusedRender.app",
        spawn=lambda *a: calls.append(a),
        running="0.5.0", installed=None,
    )
    assert started is False
    assert calls == []


def test_same_version_relaunch_skips_only_the_staleness_check():
    # fused-render://relaunch?reason=fda: a Full Disk Access grant applies to
    # the NEXT process, so respawning the very same version is the point.
    calls = []
    started = app_mod.begin_relaunch(
        quit_action=lambda on_claim=None: (on_claim(), calls.append("quit"))[1] or True,
        bundle="/Applications/FusedRender.app",
        spawn=lambda bundle, pid: calls.append(("spawn", bundle, pid)),
        running="0.5.0", installed="0.5.0",
        same_version=True, fda_granted=lambda: False,
    )
    assert started is True
    assert calls == [("spawn", "/Applications/FusedRender.app", os.getpid()), "quit"]


def test_same_version_relaunch_is_a_noop_once_this_process_has_the_grant():
    # The OS may launch a FRESH instance just to deliver the link (a second
    # click after the old pid died, or a cold start). That instance already
    # has the grant — quitting it would re-run the pidfile race the version
    # guard exists to avoid. Inconclusive (None) is also a no-op: nothing a
    # relaunch would provably change.
    for verdict in (True, None):
        calls = []
        started = app_mod.begin_relaunch(
            quit_action=lambda on_claim=None: calls.append("quit") or True,
            bundle="/Applications/FusedRender.app",
            spawn=lambda *a: calls.append(a),
            same_version=True, fda_granted=lambda: verdict,
        )
        assert started is False
        assert calls == []


def test_same_version_relaunch_still_needs_a_bundle(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    calls = []
    started = app_mod.begin_relaunch(
        quit_action=lambda on_claim=None: calls.append("quit") or True,
        spawn=lambda *a: calls.append(a),
        same_version=True, fda_granted=lambda: False,
    )
    assert started is False
    assert calls == []


def test_same_version_relaunch_joins_a_quit_already_in_flight():
    calls = []
    started = app_mod.begin_relaunch(
        quit_action=lambda on_claim=None: False,
        bundle="/Applications/FusedRender.app",
        spawn=lambda *a: calls.append(a),
        same_version=True, fda_granted=lambda: False,
    )
    assert started is False
    assert calls == []


def test_quit_action_reports_whether_it_claimed_the_teardown():
    # begin_relaunch's no-respawn guard rides this bool: the first quit from
    # any surface claims the teardown, every later one joins it.
    state = {}
    action = app_mod.make_quit_action(
        state, terminate=lambda: None,
        start=lambda *a, **k: None, remove_pidfile=lambda: None)
    assert action() is True
    assert action() is False
