"""Tests for the builtin learn mount (shell/mounts.py, D123): the bundled
learn.zip upserted into mounts.json as a read-only :archive: mount at
automount time.

FUSED_RENDER_HOME is redirected per test (same isolation as
test_shell_mounts.py); the zip path is driven through the
FUSED_RENDER_LEARN_ZIP env override — the packaged Resources/learn.zip
branch shares rclone_bin()'s frozen-app detection, covered there.
"""
import pytest

import fused_render.shell.mounts as mounts_mod


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return home


@pytest.fixture()
def learn_zip(tmp_path, monkeypatch):
    zp = tmp_path / "learn.zip"
    zp.write_bytes(b"PK\x05\x06" + b"\x00" * 18)  # empty-zip EOCD; content unused
    monkeypatch.setenv("FUSED_RENDER_LEARN_ZIP", str(zp))
    return zp


def _learn_records():
    return [m for m in mounts_mod.list_mounts()
            if m.get("builtin") == mounts_mod.LEARN_MOUNT_NAME]


# -- learn_zip_path ----------------------------------------------------------


def test_learn_zip_path_env_override(learn_zip):
    assert mounts_mod.learn_zip_path() == str(learn_zip)


def test_learn_zip_path_none_when_override_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("FUSED_RENDER_LEARN_ZIP", str(tmp_path / "gone.zip"))
    assert mounts_mod.learn_zip_path() is None


def test_learn_zip_path_none_unpackaged(monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_LEARN_ZIP", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)
    assert mounts_mod.learn_zip_path() is None


def test_learn_zip_path_packaged_bundle(tmp_path, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_LEARN_ZIP", raising=False)
    contents = tmp_path / "FusedRender.app" / "Contents"
    bundled = contents / "Resources" / "learn.zip"
    bundled.parent.mkdir(parents=True)
    bundled.write_text("")
    monkeypatch.setattr(mounts_mod.sys, "frozen", "macosx_app", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "executable",
                        str(contents / "MacOS" / "python"))
    assert mounts_mod.learn_zip_path() == str(bundled)


# -- ensure_learn_mount ------------------------------------------------------


def test_creates_builtin_record(home, learn_zip):
    mounts_mod.ensure_learn_mount()
    recs = _learn_records()
    assert len(recs) == 1
    m = recs[0]
    assert m["name"] == "learn"
    assert m["remote"] == f":archive:{learn_zip}"
    assert m["read_only"] is True
    assert m["read_only_user"] is True
    assert m["builtin"] == "learn"


def test_idempotent(home, learn_zip):
    mounts_mod.ensure_learn_mount()
    before = mounts_mod.list_mounts()
    mounts_mod.ensure_learn_mount()
    assert mounts_mod.list_mounts() == before


def test_updates_stale_remote(home, learn_zip, tmp_path, monkeypatch):
    mounts_mod.ensure_learn_mount()
    old_id = _learn_records()[0]["id"]
    moved = tmp_path / "elsewhere" / "learn.zip"
    moved.parent.mkdir()
    moved.write_bytes(learn_zip.read_bytes())
    monkeypatch.setenv("FUSED_RENDER_LEARN_ZIP", str(moved))
    mounts_mod.ensure_learn_mount()
    recs = _learn_records()
    assert len(recs) == 1
    assert recs[0]["remote"] == f":archive:{moved}"
    assert recs[0]["id"] == old_id  # updated in place, not recreated
    assert recs[0]["name"] == "learn"


def test_forces_detach_when_remote_unchanged(home, learn_zip, monkeypatch):
    # BUGBOT: an in-place app upgrade overwrites learn.zip at the SAME path,
    # so the remote string never changes — nothing must be allowed to skip
    # the detach just because the record looks unchanged, or a live rcd
    # mount from a prior run would keep serving last version's bytes.
    mounts_mod.ensure_learn_mount()
    calls = []
    monkeypatch.setattr(mounts_mod, "mounted_paths",
                        lambda: {mounts_mod.mountpoint(_learn_records()[0])})
    monkeypatch.setattr(mounts_mod, "detach_mount",
                        lambda m, force=False: calls.append(m["id"]))
    mounts_mod.ensure_learn_mount()  # same zip, same remote, still live
    assert calls == [_learn_records()[0]["id"]]


def test_stops_serve_for_old_remote_on_relocation(home, learn_zip, tmp_path, monkeypatch):
    # BUGBOT: rcd shares one VFS between a mount and its HTTP serve; a
    # detach-only fix leaves the serve wedged on the OLD fs, and sync_serves
    # would then reuse it instead of starting fresh — /api/fs/raw hangs.
    # _force_detach_learn_mount must stop the serve for the OLD remote
    # (pre-rewrite), not whatever the record's remote reads as afterward.
    mounts_mod.ensure_learn_mount()
    old_remote = _learn_records()[0]["remote"]
    monkeypatch.setattr(mounts_mod, "mounted_paths",
                        lambda: {mounts_mod.mountpoint(_learn_records()[0])})
    monkeypatch.setattr(mounts_mod, "detach_mount", lambda m, force=False: None)
    monkeypatch.setattr(mounts_mod, "_live_rcd_port", lambda: 12345)
    stopped = []
    monkeypatch.setattr(mounts_mod, "_stop_serve_for",
                        lambda port, fs: stopped.append((port, fs)))
    moved = tmp_path / "elsewhere" / "learn.zip"
    moved.parent.mkdir()
    moved.write_bytes(learn_zip.read_bytes())
    monkeypatch.setenv("FUSED_RENDER_LEARN_ZIP", str(moved))
    mounts_mod.ensure_learn_mount()
    assert stopped == [(12345, old_remote)]
    assert _learn_records()[0]["remote"] != old_remote


def test_forces_detach_on_remote_change(home, learn_zip, tmp_path, monkeypatch):
    mounts_mod.ensure_learn_mount()
    calls = []
    monkeypatch.setattr(mounts_mod, "mounted_paths",
                        lambda: {mounts_mod.mountpoint(_learn_records()[0])})
    monkeypatch.setattr(mounts_mod, "detach_mount",
                        lambda m, force=False: calls.append(m["id"]))
    moved = tmp_path / "elsewhere" / "learn.zip"
    moved.parent.mkdir()
    moved.write_bytes(learn_zip.read_bytes())
    monkeypatch.setenv("FUSED_RENDER_LEARN_ZIP", str(moved))
    mounts_mod.ensure_learn_mount()
    assert calls == [_learn_records()[0]["id"]]


def test_forces_detach_when_zip_removed(home, learn_zip, monkeypatch):
    mounts_mod.ensure_learn_mount()
    builtin_id = _learn_records()[0]["id"]
    calls = []
    monkeypatch.setattr(mounts_mod, "mounted_paths",
                        lambda: {mounts_mod.mountpoint({"name": "learn"})})
    monkeypatch.setattr(mounts_mod, "detach_mount",
                        lambda m, force=False: calls.append(m["id"]))
    monkeypatch.delenv("FUSED_RENDER_LEARN_ZIP")
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)
    mounts_mod.ensure_learn_mount()
    assert calls == [builtin_id]
    assert _learn_records() == []


def test_no_detach_when_nothing_live(home, learn_zip, monkeypatch):
    calls = []
    monkeypatch.setattr(mounts_mod, "detach_mount",
                        lambda m, force=False: calls.append(m["id"]))
    mounts_mod.ensure_learn_mount()  # first-ever create: nothing live yet
    assert calls == []


def test_removes_builtin_when_zip_gone(home, learn_zip, monkeypatch):
    mounts_mod.ensure_learn_mount()
    assert _learn_records()
    monkeypatch.delenv("FUSED_RENDER_LEARN_ZIP")
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)
    mounts_mod.ensure_learn_mount()
    assert _learn_records() == []


def test_removal_leaves_user_mounts(home, learn_zip, monkeypatch):
    user = mounts_mod.add_mount("mydata", "s3remote:bucket/prefix")
    mounts_mod.ensure_learn_mount()
    monkeypatch.delenv("FUSED_RENDER_LEARN_ZIP")
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)
    mounts_mod.ensure_learn_mount()
    assert [m["id"] for m in mounts_mod.list_mounts()] == [user["id"]]


def test_never_clobbers_user_mount_named_learn(home, learn_zip):
    user = mounts_mod.add_mount("learn", "s3remote:my-learn-bucket")
    mounts_mod.ensure_learn_mount()
    mounts = mounts_mod.list_mounts()
    assert len(mounts) == 1  # no duplicate added
    assert mounts[0]["id"] == user["id"]
    assert mounts[0]["remote"] == "s3remote:my-learn-bucket"
    assert not mounts[0].get("builtin")


def test_zip_absent_is_noop_on_empty_store(home, monkeypatch):
    monkeypatch.delenv("FUSED_RENDER_LEARN_ZIP", raising=False)
    monkeypatch.setattr(mounts_mod.sys, "frozen", None, raising=False)
    mounts_mod.ensure_learn_mount()
    assert mounts_mod.list_mounts() == []


def test_never_raises_on_storage_failure(home, learn_zip, monkeypatch):
    monkeypatch.setattr(mounts_mod, "list_mounts",
                        lambda: (_ for _ in ()).throw(OSError("disk gone")))
    mounts_mod.ensure_learn_mount()  # must swallow, not raise


# -- mount_view --------------------------------------------------------------


def test_mount_view_exposes_builtin(home, learn_zip):
    mounts_mod.ensure_learn_mount()
    user = mounts_mod.add_mount("mydata", "s3remote:bucket")
    builtin = _learn_records()[0]
    assert mounts_mod.mount_view(builtin, rcd_mounts=set(),
                                 state="disconnected")["builtin"] is True
    assert mounts_mod.mount_view(user, rcd_mounts=set(),
                                 state="disconnected")["builtin"] is False
