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
