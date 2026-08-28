"""Tests for the builtin-mount machinery (shell/mounts/automount.py, D123):
a bundled zip upserted into mounts.json as a read-only :archive: mount at
automount time. Driven through `sessions`, the one builtin the app still
ships — the mechanics are generic (BUILTIN_MOUNTS), and were written for
`learn`, which left the app for the community catalog.

FUSED_RENDER_HOME is redirected per test (same isolation as
test_shell_mounts.py); the zip path is driven through the
FUSED_RENDER_SESSIONS_ZIP env override — the packaged
Resources/sessions.zip branch shares rclone_bin()'s frozen-app detection,
covered there.
"""
import pytest

import fused_render.shell.mounts as mounts_mod


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return home


@pytest.fixture()
def sessions_zip(tmp_path, monkeypatch):
    zp = tmp_path / "sessions.zip"
    zp.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # empty-zip EOCD; content unused
    monkeypatch.setenv("FUSED_RENDER_SESSIONS_ZIP", str(zp))
    return zp


@pytest.fixture(autouse=True)
def _reset_builtin_ready():
    # _builtin_ready is process-global; reset it so readiness doesn't leak between tests.
    import fused_render.shell.mounts.automount as _am
    with _am._builtin_ready_lock:
        for name in list(_am._builtin_ready):
            _am._builtin_ready[name] = False
    yield


def _sessions_records():
    return [m for m in mounts_mod.list_mounts()
            if m.get("builtin") == mounts_mod.SESSIONS_MOUNT_NAME]


# -- builtin_zip_path ----------------------------------------------------------


def test_sessions_zip_path_env_override(sessions_zip):
    assert mounts_mod.builtin_zip_path("sessions") == str(sessions_zip)


def test_sessions_zip_path_none_when_override_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_SESSIONS_ZIP", str(tmp_path / "gone.zip"))
    assert mounts_mod.builtin_zip_path("sessions") is None


def test_sessions_zip_path_none_unpackaged(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_SESSIONS_ZIP", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)
    assert mounts_mod.builtin_zip_path("sessions") is None


def test_sessions_zip_path_packaged_bundle(tmp_path, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_SESSIONS_ZIP", raising=False)
    contents = tmp_path / "FusedRender.app" / "Contents"
    bundled = contents / "Resources" / "sessions.zip"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("")
    monkeypatch.setattr(mounts_mod.sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "executable",
                        str(contents / "MacOS" / "python"))
    assert mounts_mod.builtin_zip_path("sessions") == str(bundled)


def test_sessions_zip_path_runtime_adjacent(tmp_path, monkeypatch):
    # Windows/Linux payload layout: the zip sits next to the bundled runtime
    # (payload/python/pythonw.exe -> payload/assets/sessions.zip), so the server
    # resolves it without the supervisor-injected env var.
    monkeypatch.delenv("FUSED_RENDER_SESSIONS_ZIP", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)
    payload = tmp_path / "payload"
    zp = payload / "assets" / "sessions.zip"
    zp.parent.mkdir(parents=True)
    zp.write_text("")
    monkeypatch.setattr(mounts_mod.sys, "executable",
                        str(payload / "python" / "pythonw.exe"))
    assert mounts_mod.builtin_zip_path("sessions") == str(zp)


# -- ensure_builtin_mounts ------------------------------------------------------


def test_creates_builtin_record(home, sessions_zip):
    mounts_mod.ensure_builtin_mounts()
    recs = _sessions_records()
    assert len(recs) == 1
    m = recs[0]
    assert m["name"] == "sessions"
    assert m["remote"] == f":archive:{sessions_zip}"
    assert m["read_only"] is True
    assert m["read_only_user"] is True
    assert m["builtin"] == "sessions"


def test_idempotent(home, sessions_zip):
    mounts_mod.ensure_builtin_mounts()
    before = mounts_mod.list_mounts()
    mounts_mod.ensure_builtin_mounts()
    assert mounts_mod.list_mounts() == before


def test_builtin_mount_ready_reads_flag_not_live_probe(home, sessions_zip, monkeypatch):
    # builtin readiness must never do a live mount probe (a cold-start _ismount blocked /api/config ~60s); blow up if it does and drive readiness off the flag.
    mounts_mod.ensure_builtin_mounts()

    def _boom(*_a, **_k):
        raise AssertionError("live mount probe must not run on the readiness path")

    monkeypatch.setattr(mounts_mod, "mounted_paths", _boom)

    assert mounts_mod.sessions_mount_ready() is False  # not attached this run yet

    mounts_mod.set_builtin_ready("sessions", True)
    assert mounts_mod.sessions_mount_ready() is True

    mounts_mod.set_builtin_ready("sessions", False)
    assert mounts_mod.sessions_mount_ready() is False


def test_updates_stale_remote(home, sessions_zip, tmp_path, monkeypatch):
    mounts_mod.ensure_builtin_mounts()
    old_id = _sessions_records()[0]["id"]
    moved = tmp_path / "elsewhere" / "sessions.zip"
    moved.parent.mkdir()
    moved.write_bytes(sessions_zip.read_bytes())
    monkeypatch.setenv("FUSED_RENDER_SESSIONS_ZIP", str(moved))
    mounts_mod.ensure_builtin_mounts()
    recs = _sessions_records()
    assert len(recs) == 1
    assert recs[0]["remote"] == f":archive:{moved}"
    assert recs[0]["id"] == old_id  # updated in place, not recreated
    assert recs[0]["name"] == "sessions"


def test_forces_detach_when_remote_unchanged(home, sessions_zip, monkeypatch):
    # BUGBOT: an in-place app upgrade overwrites the zip at the SAME path,
    # so the remote string never changes — nothing must be allowed to skip
    # the detach just because the record looks unchanged, or a live rcd
    # mount from a prior run would keep serving last version's bytes.
    mounts_mod.ensure_builtin_mounts()
    calls = []
    monkeypatch.setattr(mounts_mod, "mounted_paths",
                        lambda: {mounts_mod.mountpoint(_sessions_records()[0])})
    monkeypatch.setattr(mounts_mod, "detach_mount",
                        lambda m, force=False: calls.append((m["id"], force)))
    mounts_mod.ensure_builtin_mounts()  # same zip, same remote, still live
    assert calls == [(_sessions_records()[0]["id"], True)]


def test_force_detach_passes_force_true(home, sessions_zip, monkeypatch):
    # BUGBOT: detach_mount's default (force=False) deliberately leaves a
    # non-busy failure in place (rcd down but the kernel mount survives, a
    # busy-retry that still fails, ...) — right for an explicit user
    # unmount, but it would let attach_mount adopt that stale kernel mount
    # as a "foreign" one instead of remounting, defeating the whole point
    # of this forced-refresh path.
    mounts_mod.ensure_builtin_mounts()
    calls = []
    monkeypatch.setattr(mounts_mod, "mounted_paths",
                        lambda: {mounts_mod.mountpoint(_sessions_records()[0])})
    monkeypatch.setattr(mounts_mod, "detach_mount",
                        lambda m, force=False: calls.append(force))
    mounts_mod.ensure_builtin_mounts()
    assert calls == [True]


def test_force_unmounts_kernel_mount_surviving_a_successful_detach(
        home, sessions_zip, monkeypatch):
    # BUGBOT: detach_mount(force=True) only escalates to _force_unmount when
    # the rc mount/unmount call itself FAILS — it never rechecks
    # os.path.ismount after a call that reports success. On macOS (nfsmount),
    # rc can report success while the kernel NFS mount lingers regardless
    # (reconnect_mount already guards against exactly this). Simulate that:
    # detach_mount "succeeds" (returns None) but the kernel mount is still
    # there afterward — _force_detach_builtin_mount must force-unmount it too.
    mounts_mod.ensure_builtin_mounts()
    monkeypatch.setattr(mounts_mod, "mounted_paths",
                        lambda: {mounts_mod.mountpoint(_sessions_records()[0])})
    monkeypatch.setattr(mounts_mod, "detach_mount", lambda m, force=False: None)
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: True)
    calls = []
    monkeypatch.setattr(mounts_mod, "_force_unmount",
                        lambda mp: calls.append(mp))
    mounts_mod.ensure_builtin_mounts()
    assert calls == [mounts_mod.mountpoint(_sessions_records()[0])]


def test_clears_rcd_bookkeeping_after_force_unmount(home, sessions_zip, monkeypatch):
    # BUGBOT: _force_unmount operates purely at the kernel level (umount /
    # diskutil) — it never tells rcd anything, so a successful force-unmount
    # can leave rcd's OWN mount/listmounts bookkeeping still claiming the
    # mountpoint. run_automount's loop treats exactly that combination (rcd
    # still lists it, kernel does not) as split-brain and skips
    # attach_mount entirely for it — the builtin mount would never get
    # remounted after this very refresh. A follow-up rc mount/unmount call
    # (mirroring reconnect_mount's own pattern) must clear rcd's
    # bookkeeping too, so run_automount's next mounted_paths() snapshot no
    # longer lists a mountpoint the kernel has already dropped.
    mounts_mod.ensure_builtin_mounts()
    mp = mounts_mod.mountpoint(_sessions_records()[0])
    monkeypatch.setattr(mounts_mod, "mounted_paths", lambda: {mp})
    monkeypatch.setattr(mounts_mod, "detach_mount", lambda m, force=False: None)
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: True)
    monkeypatch.setattr(mounts_mod, "_force_unmount", lambda p: None)
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda: 12345)
    rc_calls = []
    monkeypatch.setattr(
        mounts_mod, "_rc",
        lambda port, method, params=None, timeout=30: (
            rc_calls.append((port, method, params)) or {}
        ),
    )
    mounts_mod.ensure_builtin_mounts()
    assert (12345, "mount/unmount", {"mountPoint": mp}) in rc_calls


def test_no_force_unmount_when_kernel_mount_already_gone(
        home, sessions_zip, monkeypatch):
    mounts_mod.ensure_builtin_mounts()
    monkeypatch.setattr(mounts_mod, "mounted_paths",
                        lambda: {mounts_mod.mountpoint(_sessions_records()[0])})
    monkeypatch.setattr(mounts_mod, "detach_mount", lambda m, force=False: None)
    monkeypatch.setattr(mounts_mod.os.path, "ismount", lambda p: False)
    calls = []
    monkeypatch.setattr(mounts_mod, "_force_unmount",
                        lambda mp: calls.append(mp))
    mounts_mod.ensure_builtin_mounts()
    assert calls == []


def test_stops_serve_for_old_remote_on_relocation(home, sessions_zip, tmp_path, monkeypatch):
    # BUGBOT: rcd shares one VFS between a mount and its HTTP serve; a
    # detach-only fix leaves the serve wedged on the OLD fs, and sync_serves
    # would then reuse it instead of starting fresh — /api/fs/raw hangs.
    # _force_detach_builtin_mount must stop the serve for the OLD remote
    # (pre-rewrite), not whatever the record's remote reads as afterward.
    mounts_mod.ensure_builtin_mounts()
    old_remote = _sessions_records()[0]["remote"]
    monkeypatch.setattr(mounts_mod, "mounted_paths",
                        lambda: {mounts_mod.mountpoint(_sessions_records()[0])})
    monkeypatch.setattr(mounts_mod, "detach_mount", lambda m, force=False: None)
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda: 12345)
    stopped = []
    monkeypatch.setattr(mounts_mod, "_stop_serve_for",
                        lambda port, fs: stopped.append((port, fs)))
    moved = tmp_path / "elsewhere" / "sessions.zip"
    moved.parent.mkdir()
    moved.write_bytes(sessions_zip.read_bytes())
    monkeypatch.setenv("FUSED_RENDER_SESSIONS_ZIP", str(moved))
    mounts_mod.ensure_builtin_mounts()
    assert stopped == [(12345, old_remote)]
    assert _sessions_records()[0]["remote"] != old_remote


def test_forces_detach_on_remote_change(home, sessions_zip, tmp_path, monkeypatch):
    mounts_mod.ensure_builtin_mounts()
    calls = []
    monkeypatch.setattr(mounts_mod, "mounted_paths",
                        lambda: {mounts_mod.mountpoint(_sessions_records()[0])})
    monkeypatch.setattr(mounts_mod, "detach_mount",
                        lambda m, force=False: calls.append(m["id"]))
    moved = tmp_path / "elsewhere" / "sessions.zip"
    moved.parent.mkdir()
    moved.write_bytes(sessions_zip.read_bytes())
    monkeypatch.setenv("FUSED_RENDER_SESSIONS_ZIP", str(moved))
    mounts_mod.ensure_builtin_mounts()
    assert calls == [_sessions_records()[0]["id"]]


def test_forces_detach_when_zip_removed(home, sessions_zip, monkeypatch):
    mounts_mod.ensure_builtin_mounts()
    builtin_id = _sessions_records()[0]["id"]
    calls = []
    monkeypatch.setattr(mounts_mod, "mounted_paths",
                        lambda: {mounts_mod.mountpoint({"name": "sessions"})})
    monkeypatch.setattr(mounts_mod, "detach_mount",
                        lambda m, force=False: calls.append(m["id"]))
    sessions_zip.unlink()
    monkeypatch.delenv("FUSED_RENDER_SESSIONS_ZIP")
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)
    mounts_mod.ensure_builtin_mounts()
    assert calls == [builtin_id]
    assert _sessions_records() == []


def test_no_detach_when_nothing_live(home, sessions_zip, monkeypatch):
    calls = []
    monkeypatch.setattr(mounts_mod, "detach_mount",
                        lambda m, force=False: calls.append(m["id"]))
    mounts_mod.ensure_builtin_mounts()  # first-ever create: nothing live yet
    assert calls == []


def test_removes_builtin_when_zip_gone(home, sessions_zip, monkeypatch):
    mounts_mod.ensure_builtin_mounts()
    assert _sessions_records()
    sessions_zip.unlink()
    monkeypatch.delenv("FUSED_RENDER_SESSIONS_ZIP")
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)
    mounts_mod.ensure_builtin_mounts()
    assert _sessions_records() == []


def test_keeps_builtin_when_zip_unresolvable_but_present(home, sessions_zip, monkeypatch):
    # A dev-checkout server sharing the real home resolves no zip of its own,
    # but the record's zip is still on disk — it must NOT delete the packaged
    # app's valid record (the incident where the sidebar entry it gated
    # vanished).
    mounts_mod.ensure_builtin_mounts()
    record = _sessions_records()[0]
    monkeypatch.delenv("FUSED_RENDER_SESSIONS_ZIP")
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)
    mounts_mod.ensure_builtin_mounts()
    assert _sessions_records() == [record]


def test_removal_leaves_user_mounts(home, sessions_zip, monkeypatch):
    user = mounts_mod.add_mount("mydata", "s3remote:bucket/prefix")
    mounts_mod.ensure_builtin_mounts()
    sessions_zip.unlink()
    monkeypatch.delenv("FUSED_RENDER_SESSIONS_ZIP")
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)
    mounts_mod.ensure_builtin_mounts()
    assert [m["id"] for m in mounts_mod.list_mounts()] == [user["id"]]


# -- retired builtins (a name that left BUILTIN_MOUNTS) -----------------------


def _retired_learn_record() -> dict:
    """What an install upgraded past D419 already has in its mounts.json."""
    return {
        "id": "learn0000001",
        "name": "learn",
        "remote": ":archive:/Applications/FusedRender.app/Contents/Resources/learn.zip",
        "read_only": True,
        "read_only_user": True,
        "builtin": "learn",
    }


def test_prunes_the_record_of_a_retired_builtin(home, sessions_zip):
    # BUGBOT: dropping `learn` from BUILTIN_MOUNTS left every already-installed
    # machine with a record nothing visits — _ensure_builtin_mount's
    # remove-when-the-zip-is-gone branch only runs for names still in the dict.
    # run_automount then failed to attach it on every startup (the upgrade took
    # its zip out of the bundle), and delete_mount 400s on any `builtin`
    # marker, so the broken row was permanent and unremovable.
    from fused_render.shell.mounts.store import _write
    _write([_retired_learn_record()])
    mounts_mod.ensure_builtin_mounts()
    assert [m["name"] for m in mounts_mod.list_mounts()] == ["sessions"]


def test_pruning_a_retired_builtin_detaches_it_and_stops_its_serve(
        home, sessions_zip, monkeypatch):
    # The record going is not enough: rcd survives server restarts, so the old
    # mount and the serve bound to its remote outlive it unless torn down.
    from fused_render.shell.mounts.store import _write
    record = _retired_learn_record()
    _write([record])
    mp = mounts_mod.mountpoint(record)
    detached, stopped = [], []
    monkeypatch.setattr(mounts_mod, "mounted_paths", lambda: {mp})
    monkeypatch.setattr(mounts_mod, "detach_mount",
                        lambda m, force=False: detached.append((m["id"], force)))
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda: 5572)
    monkeypatch.setattr(mounts_mod, "_stop_serve_for",
                        lambda port, remote: stopped.append(remote))
    mounts_mod.ensure_builtin_mounts()
    assert detached == [(record["id"], True)]
    assert stopped == [record["remote"]]


def test_pruning_leaves_user_mounts_and_live_builtins(home, sessions_zip):
    from fused_render.shell.mounts.store import _write
    user = mounts_mod.add_mount("mydata", "s3remote:bucket/prefix")
    mounts_mod.ensure_builtin_mounts()
    _write(mounts_mod.list_mounts() + [_retired_learn_record()])
    mounts_mod.ensure_builtin_mounts()
    assert sorted(m["name"] for m in mounts_mod.list_mounts()) == ["mydata", "sessions"]
    assert [m for m in mounts_mod.list_mounts() if m["id"] == user["id"]]


def test_pruning_never_touches_a_user_mount_named_after_a_retired_builtin(
        home, sessions_zip):
    # No `builtin` marker => not ours, whatever it is called.
    user = mounts_mod.add_mount("learn", "s3remote:my-learn-bucket")
    mounts_mod.ensure_builtin_mounts()
    kept = [m for m in mounts_mod.list_mounts() if m["id"] == user["id"]]
    assert len(kept) == 1
    assert kept[0]["remote"] == "s3remote:my-learn-bucket"


def test_never_clobbers_user_mount_named_sessions(home, sessions_zip):
    user = mounts_mod.add_mount("sessions", "s3remote:my-sessions-bucket")
    mounts_mod.ensure_builtin_mounts()
    mounts = mounts_mod.list_mounts()
    assert len(mounts) == 1  # no duplicate added
    assert mounts[0]["id"] == user["id"]
    assert mounts[0]["remote"] == "s3remote:my-sessions-bucket"
    assert not mounts[0].get("builtin")


def test_zip_absent_is_noop_on_empty_store(home, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_SESSIONS_ZIP", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)
    mounts_mod.ensure_builtin_mounts()
    assert mounts_mod.list_mounts() == []


def test_never_raises_on_storage_failure(home, sessions_zip, monkeypatch):
    monkeypatch.setattr(mounts_mod, "list_mounts",
                        lambda: (_ for _ in ()).throw(OSError("disk gone")))
    mounts_mod.ensure_builtin_mounts()  # must swallow, not raise


def test_force_detach_runs_outside_store_lock(home, sessions_zip, monkeypatch):
    # BUGBOT: rcd I/O (detach_mount, _stop_serve_for) must never run while
    # _store_lock is held — every mount create/delete/update takes the same
    # lock, and rcd I/O under it would stall them for the full rc timeout.
    mounts_mod.ensure_builtin_mounts()
    monkeypatch.setattr(mounts_mod, "mounted_paths",
                        lambda: {mounts_mod.mountpoint(_sessions_records()[0])})

    def fake_detach(m, force=False):
        # Locked() has no public accessor; RLock would silently allow
        # reentry and mask the bug, but _store_lock is a plain Lock, so
        # acquire(blocking=False) genuinely fails only if something else
        # (this very call, if still under the lock) already holds it.
        assert mounts_mod._store_lock.acquire(blocking=False), (
            "_store_lock was still held during force-detach I/O"
        )
        mounts_mod._store_lock.release()

    monkeypatch.setattr(mounts_mod, "detach_mount", fake_detach)
    mounts_mod.ensure_builtin_mounts()


# -- sessions_mount_ready --------------------------------------------------------


def test_sessions_mount_ready_false_until_actually_mounted(home, sessions_zip):
    # Record presence alone isn't "ready" — the flag stays False until run_automount attaches it this run.
    assert mounts_mod.sessions_mount_ready() is False
    mounts_mod.ensure_builtin_mounts()
    assert mounts_mod.sessions_mount_ready() is False  # record exists, not attached


def test_sessions_mount_ready_true_once_attached_this_run(home, sessions_zip):
    mounts_mod.ensure_builtin_mounts()
    mounts_mod.set_builtin_ready("sessions", True)  # what run_automount does on a successful attach
    assert mounts_mod.sessions_mount_ready() is True


def test_sessions_mount_ready_false_without_zip(home):
    assert mounts_mod.sessions_mount_ready() is False


def test_run_automount_marks_builtin_ready_only_after_attach(home, sessions_zip, monkeypatch):
    # A stale True from a previous run must be cleared before the remount and re-set only after this run's attach succeeds.
    import fused_render.shell.mounts.health as health_mod

    mounts_mod.set_builtin_ready("sessions", True)  # stale, from a "previous run"
    seen_during_attach = []

    def fake_attach(m):
        seen_during_attach.append(mounts_mod.sessions_mount_ready())
        return None  # success

    monkeypatch.setattr(health_mod, "attach_mount", fake_attach)
    monkeypatch.setattr(health_mod, "sync_serves", lambda: None)
    monkeypatch.setattr(mounts_mod, "mounted_paths", lambda: set())

    mounts_mod.run_automount()

    assert seen_during_attach == [False]              # cleared before the attach
    assert mounts_mod.sessions_mount_ready() is True      # set only after it succeeded


def test_run_automount_leaves_builtin_not_ready_on_attach_failure(home, sessions_zip, monkeypatch):
    import fused_render.shell.mounts.health as health_mod

    mounts_mod.set_builtin_ready("sessions", True)  # stale
    monkeypatch.setattr(health_mod, "attach_mount", lambda m: "mount failed")
    monkeypatch.setattr(health_mod, "sync_serves", lambda: None)
    monkeypatch.setattr(mounts_mod, "mounted_paths", lambda: set())

    mounts_mod.run_automount()
    assert mounts_mod.sessions_mount_ready() is False


def test_poll_once_never_marks_builtin_ready_from_observation(home, sessions_zip, monkeypatch):
    # poll_once must never set readiness True off an observed "mounted" (a lingering/prior-run mount reads mounted while stale); True is only ever a real attach this run.
    mounts_mod.ensure_builtin_mounts()
    monkeypatch.setattr(mounts_mod, "mounted_paths", lambda: set())
    monkeypatch.setattr(mounts_mod, "mount_state", lambda m, live, **k: "mounted")
    mounts_mod.poll_once()
    assert mounts_mod.sessions_mount_ready() is False


def test_poll_once_leaves_ready_flag_untouched(home, sessions_zip, monkeypatch):
    # The health monitor must not clear a readiness set by a real attach off a transient not-mounted snapshot (nothing would restore it).
    mounts_mod.ensure_builtin_mounts()
    mounts_mod.set_builtin_ready("sessions", True)
    monkeypatch.setattr(mounts_mod, "mounted_paths", lambda: set())
    monkeypatch.setattr(mounts_mod, "mount_state", lambda m, live, **k: "disconnected")
    mounts_mod.poll_once()
    assert mounts_mod.sessions_mount_ready() is True


def test_reconnect_marks_builtin_ready_on_success(home, sessions_zip, monkeypatch):
    # A manual Reconnect (the out-of-band repair automount defers to) must flip the flag on success, and only on success.
    import fused_render.shell.mounts.lifecycle as lifecycle_mod
    mounts_mod.ensure_builtin_mounts()
    m = _sessions_records()[0]

    monkeypatch.setattr(mounts_mod, "_winfsp_available", lambda: True)
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda: None)
    monkeypatch.setattr(lifecycle_mod, "_is_mounted", lambda mp: False)
    monkeypatch.setattr(lifecycle_mod, "attach_mount", lambda mm: None)  # success
    assert lifecycle_mod.reconnect_mount(m) is None
    assert mounts_mod.sessions_mount_ready() is True

    mounts_mod.set_builtin_ready("sessions", False)
    monkeypatch.setattr(lifecycle_mod, "attach_mount", lambda mm: "still broken")
    assert lifecycle_mod.reconnect_mount(m) == "still broken"
    assert mounts_mod.sessions_mount_ready() is False  # failed reconnect: unchanged


def test_sessions_mount_ready_false_for_user_mount_named_sessions(home):
    # A user mount named "sessions" has no builtin marker, so the flag is never set for it.
    mounts_mod.add_mount("sessions", "s3remote:my-sessions-bucket")
    mounts_mod.set_builtin_ready("sessions", False)
    assert mounts_mod.sessions_mount_ready() is False


# -- mount_view --------------------------------------------------------------


def test_mount_view_exposes_builtin(home, sessions_zip):
    mounts_mod.ensure_builtin_mounts()
    user = mounts_mod.add_mount("mydata", "s3remote:bucket")
    builtin = _sessions_records()[0]
    assert mounts_mod.mount_view(builtin, rcd_mounts=set(),
                                 state="disconnected")["builtin"] is True
    assert mounts_mod.mount_view(user, rcd_mounts=set(),
                                 state="disconnected")["builtin"] is False
