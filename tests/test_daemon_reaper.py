"""Tests for the rcd daemon reaper (shell/mounts.reap_stale_rcd) and the
guard primitives the pytest session-teardown fixture (tests/conftest.py
_reap_test_rcd_daemons) relies on.

The rcd daemon is spawned detached and outlives the server on purpose, with no
reaper — so finished pytest runs and deleted worktrees leave orphaned daemons
alive for days. These tests exercise reaping WITHOUT touching real system
processes: the only process we ever signal is a short-lived subprocess we spawn
ourselves, and the rclone-identity guard is monkeypatched for it.
"""
import os
import subprocess
import sys
import time

import pytest

import fused_render.shell.mounts as mounts_mod


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Redirect FUSED_RENDER_HOME (and hence the registry path) into tmp."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    monkeypatch.delenv("FUSED_RENDER_BRANCH", raising=False)
    return home


def _spawn_sleeper():
    """A real, harmless child process to stand in for a leaked rcd — so a
    SIGTERM lands on something we own, never a system process."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def _wait_dead(proc, timeout=5.0):
    try:
        proc.wait(timeout=timeout)
        return True
    except subprocess.TimeoutExpired:
        return False


def test_reap_kills_orphan_with_gone_dir_and_keeps_live(home, tmp_path, monkeypatch):
    # A daemon whose home dir is gone (orphan) alongside one whose home dir
    # still exists (in use). Only the orphan may be killed.
    orphan = _spawn_sleeper()
    try:
        gone_home = str(tmp_path / "gone")  # never created -> dir is absent
        live_home = str(tmp_path / "live")
        os.makedirs(live_home)

        # The orphan's rc port is dead, so _confirmed_our_rcd must fall through
        # to the (monkeypatched) command-line identity check.
        monkeypatch.setattr(
            mounts_mod, "_pid_looks_like_rcd",
            lambda pid: pid == orphan.pid,
        )

        registry = [
            {"pid": orphan.pid, "port": 0, "dir": gone_home},
            {"pid": os.getpid(), "port": 0, "dir": live_home},  # this test proc
        ]
        mounts_mod.storage.write_json(mounts_mod._rcd_registry_path(), registry)

        mounts_mod.reap_stale_rcd()

        # Orphan was signalled and actually terminated.
        assert _wait_dead(orphan), "orphaned daemon should have been SIGTERM'd"

        # Registry now holds only the live entry; orphan entry was pruned.
        remaining = mounts_mod.storage.read_json(mounts_mod._rcd_registry_path())
        assert remaining == [{"pid": os.getpid(), "port": 0, "dir": live_home}]
    finally:
        if orphan.poll() is None:
            orphan.kill()
            orphan.wait()


def test_reap_leaves_orphan_it_cannot_identify(home, tmp_path, monkeypatch):
    # Home dir gone AND pid alive, but NOT provably our rclone rcd -> must NOT
    # be killed, and its entry must be retained for a later run.
    survivor = _spawn_sleeper()
    try:
        gone_home = str(tmp_path / "gone")
        monkeypatch.setattr(mounts_mod, "_pid_looks_like_rcd", lambda pid: False)
        entry = {"pid": survivor.pid, "port": 0, "dir": gone_home}
        mounts_mod.storage.write_json(mounts_mod._rcd_registry_path(), [entry])

        mounts_mod.reap_stale_rcd()

        assert survivor.poll() is None, "unidentifiable process must not be killed"
        remaining = mounts_mod.storage.read_json(mounts_mod._rcd_registry_path())
        assert remaining == [entry]
    finally:
        survivor.kill()
        survivor.wait()


def test_reap_drops_dead_pid_entry_without_killing(home, tmp_path):
    # Home dir gone and pid already dead -> just clean the stale registry entry.
    dead = _spawn_sleeper()
    dead.kill()
    dead.wait()
    gone_home = str(tmp_path / "gone")
    mounts_mod.storage.write_json(
        mounts_mod._rcd_registry_path(),
        [{"pid": dead.pid, "port": 0, "dir": gone_home}],
    )

    mounts_mod.reap_stale_rcd()

    assert mounts_mod.storage.read_json(mounts_mod._rcd_registry_path()) == []


def test_write_rcd_state_registers_daemon(home):
    # write_rcd_state must record the daemon in the central registry keyed by
    # home, so the reaper can find it after the home dir is gone.
    mounts_mod.write_rcd_state(12345, 999, log_path=str(home / "rcd.log"))
    reg = mounts_mod.storage.read_json(mounts_mod._rcd_registry_path())
    assert reg == [{"pid": 999, "port": 12345, "dir": str(home)}]

    # A re-spawn for the same home replaces (not duplicates) the entry.
    mounts_mod.write_rcd_state(23456, 1001, log_path=str(home / "rcd.log"))
    reg = mounts_mod.storage.read_json(mounts_mod._rcd_registry_path())
    assert reg == [{"pid": 1001, "port": 23456, "dir": str(home)}]


def test_pid_alive_and_identity_guard(home, monkeypatch):
    # The primitives the conftest teardown fixture gates every kill on.
    proc = _spawn_sleeper()
    try:
        assert mounts_mod._pid_alive(proc.pid) is True
        assert mounts_mod._pid_alive(0) is False
    finally:
        proc.kill()
        proc.wait()
    assert mounts_mod._pid_alive(proc.pid) is False


def test_conftest_teardown_guard_terminates_tracked_pid(tmp_path, monkeypatch):
    # Mirror the conftest fixture's teardown decision: a tracked pid recorded
    # under a temp home, still alive, provably rcd -> SIGTERM. Verifies the
    # exact guard logic terminates a real (self-spawned) tracked process.
    import signal
    import tempfile

    proc = _spawn_sleeper()
    try:
        monkeypatch.setattr(
            mounts_mod, "_pid_looks_like_rcd", lambda pid: pid == proc.pid
        )
        # home under the system temp root, as every test home is.
        home = tempfile.mkdtemp(dir=tempfile.gettempdir())
        tracked = [(proc.pid, home)]

        tmp_root = os.path.realpath(tempfile.gettempdir())
        for pid, h in tracked:
            if not os.path.realpath(str(h)).startswith(tmp_root):
                continue
            if mounts_mod._pid_alive(pid) and mounts_mod._pid_looks_like_rcd(pid):
                os.kill(pid, signal.SIGTERM)

        assert _wait_dead(proc), "tracked test daemon should be terminated"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
