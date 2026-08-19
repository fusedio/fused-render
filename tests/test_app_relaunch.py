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
    assert "open -a" in script
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
    """The regression D355 could have introduced, pinned deterministically.

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


def test_quit_action_reports_whether_it_claimed_the_teardown():
    # begin_relaunch's no-respawn guard rides this bool: the first quit from
    # any surface claims the teardown, every later one joins it.
    state = {}
    action = app_mod.make_quit_action(
        state, terminate=lambda: None,
        start=lambda *a, **k: None, remove_pidfile=lambda: None)
    assert action() is True
    assert action() is False
