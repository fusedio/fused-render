"""rcd ownership gating on quit: `stop_local_rcd` must only kill the shared
rcd daemon when this process spawned it or the spawner is gone. rcd.json
records the spawner's pid at spawn time; a macOS app quit must not SIGTERM an
rcd that a still-running CLI server (a different, live spawner) owns. Old
rcd.json files without a spawner_pid keep the previous behavior (kill).
No real rclone is ever exec'd — same monkeypatch style as
tests/test_mounts_rcd_persist.py."""
import os

import pytest

import fused_render.shell.mounts as mounts_mod


# ---- write path: rcd.json records who spawned the daemon ---------------------


def test_write_rcd_state_records_spawner_pid(monkeypatch):
    written = {}

    monkeypatch.setattr(
        mounts_mod.storage, "write_json", lambda path, data: written.update(data)
    )
    monkeypatch.setattr(mounts_mod, "_register_rcd", lambda *a, **k: None)

    mounts_mod.write_rcd_state(5572, 4321, "/fake/rcd.log")

    assert written["port"] == 5572
    assert written["pid"] == 4321
    assert written["spawner_pid"] == os.getpid()


# ---- stop path: only kill what we own (or what nobody owns anymore) ----------


@pytest.fixture
def stop_ctx(monkeypatch):
    """Common stop_local_rcd harness: persistence off, entry injectable,
    _kill_current_rcd recorded instead of executed."""
    ctx = {"entry": None, "killed": [], "alive_pids": set()}

    monkeypatch.setattr(mounts_mod, "_rclone_should_persist", lambda: False)
    monkeypatch.setattr(
        mounts_mod.storage, "read_json", lambda path: ctx["entry"]
    )
    monkeypatch.setattr(
        mounts_mod, "_kill_current_rcd", lambda: ctx["killed"].append(True)
    )
    monkeypatch.setattr(
        mounts_mod, "_pid_alive", lambda pid: pid in ctx["alive_pids"]
    )
    return ctx


def test_stop_skips_when_spawner_is_alive_and_not_us(stop_ctx):
    other = os.getpid() + 1
    stop_ctx["entry"] = {"port": 5572, "pid": 4321, "spawner_pid": other}
    stop_ctx["alive_pids"] = {other}

    mounts_mod.stop_local_rcd()

    assert stop_ctx["killed"] == []  # the CLI server's rcd is not ours to stop


def test_stop_kills_when_we_are_the_spawner(stop_ctx):
    stop_ctx["entry"] = {"port": 5572, "pid": 4321, "spawner_pid": os.getpid()}
    stop_ctx["alive_pids"] = {os.getpid()}

    mounts_mod.stop_local_rcd()

    assert stop_ctx["killed"] == [True]


def test_stop_kills_when_spawner_is_gone(stop_ctx):
    other = os.getpid() + 1
    stop_ctx["entry"] = {"port": 5572, "pid": 4321, "spawner_pid": other}
    stop_ctx["alive_pids"] = set()  # spawner exited; the daemon is orphaned

    mounts_mod.stop_local_rcd()

    assert stop_ctx["killed"] == [True]


def test_stop_kills_when_spawner_pid_missing(stop_ctx):
    # Old rcd.json written before spawner_pid existed → preserve the previous
    # behavior (kill), don't strand the daemon forever.
    stop_ctx["entry"] = {"port": 5572, "pid": 4321}

    mounts_mod.stop_local_rcd()

    assert stop_ctx["killed"] == [True]


def test_stop_kills_when_no_entry_at_all(stop_ctx):
    # No rcd.json: _kill_current_rcd itself no-ops on a missing entry, but the
    # ownership gate must not block the call path.
    stop_ctx["entry"] = None

    mounts_mod.stop_local_rcd()

    assert stop_ctx["killed"] == [True]
