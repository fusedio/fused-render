"""Linux in-app updater (fused_render/update/linux.py) and its stamp file.

Mirrors tests/test_mac_update.py's shape for the state machine (shared with
mac.py through update/_manager.py — that machinery is already covered there),
adding what's actually new on Linux: `method()`'s appimage/none decision, the
writability refusal, the swap onto a temp "AppImage" via os.replace, a cancel
during download, a checksum mismatch leaving the original untouched, the
version-stamp write, and `_disk_version` reading it back.
"""
import os
import time

import pytest

from fused_render import jobs
from fused_render.update import common, linux
from fused_render import installed


# ---- method() -----------------------------------------------------------


def test_method_none_when_not_running_from_an_appimage():
    manager = linux.UpdateManager(bundle=None, method=None)
    assert manager.method() == "none"


def test_method_appimage_when_running_from_one(tmp_path):
    appimage = tmp_path / "FusedRender.AppImage"
    appimage.write_bytes(b"x")
    manager = linux.UpdateManager(bundle=str(appimage), method=None)
    assert manager.method() == "appimage"


# ---- UpdateManager state machine (check()) -------------------------------


def _manager(monkeypatch, *, available=None, current="0.4.10", bundle="/nonexistent/FusedRender.AppImage"):
    manager = linux.UpdateManager(bundle=bundle, method="appimage")
    monkeypatch.setattr(linux, "__version__", current)
    if available is not None:
        manifest = {"schema": 1, "version": available,
                    "url": "https://x/y.AppImage", "sha256": "s", "signature": "g"}
        monkeypatch.setattr(common, "fetch_manifest", lambda url, **kwargs: dict(manifest))
    return manager


def test_check_finds_newer(monkeypatch):
    manager = _manager(monkeypatch, available="9.9.9")
    status = manager.check()
    assert status["state"] == "available"
    assert status["latest_version"] == "9.9.9"
    assert status["method"] == "appimage"


def test_check_up_to_date(monkeypatch):
    manager = _manager(monkeypatch, available="0.0.1")
    status = manager.check()
    assert status["state"] == "idle"
    assert status["latest_version"] is None


def test_running_version_reads_the_patched_module_seam(monkeypatch):
    # `monkeypatch.setattr(linux, "__version__", ...)` rebinds the name in
    # `linux`'s own module namespace, not `fused_render.__version__` — a
    # comparison that reads the package attribute instead would never see
    # this patch. Picking a `current` ABOVE the manifest's version, while the
    # real `fused_render.__version__` sits BELOW it, makes the two seams
    # disagree on the outcome: only a manager that actually reads the
    # patched value lands on "idle" here.
    manager = _manager(monkeypatch, available="9.9.9", current="10.0.0")
    status = manager.check()
    assert status["state"] == "idle"
    assert status["latest_version"] is None


def test_start_noop_when_unpackaged(monkeypatch):
    monkeypatch.setattr(linux, "_manager", None)
    monkeypatch.delenv(linux.DEV_MANAGER_ENV, raising=False)
    monkeypatch.setattr(linux.startup, "appimage_path", lambda: None)
    assert linux.start() is None
    assert linux.manager() is None


def test_dev_run_gets_a_check_only_manager_when_asked(monkeypatch):
    monkeypatch.setattr(linux, "_manager", None)
    monkeypatch.setattr(linux.startup, "appimage_path", lambda: None)
    monkeypatch.setenv(linux.DEV_MANAGER_ENV, "1")
    monkeypatch.setattr(linux.UpdateManager, "start_auto_checks", lambda self: None)
    manager = linux.start()
    assert manager is not None and linux.manager() is manager
    status = manager.status()
    assert status["check_only"] is True
    assert status["method"] == "none"


# ---- the AppImage swap ----------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_jobs():
    jobs.reset()
    yield
    jobs.reset()


def _appimage_manager(monkeypatch, tmp_path, *, available="9.9.9", current="0.4.10",
                      content=b"old-bytes"):
    """A manager whose target AppImage is a real file inside tmp_path, its
    parent dir doubling as the updates dir — so `_install_appimage` can run
    for real without touching anything outside tmp_path."""
    appimage = tmp_path / "FusedRender.AppImage"
    appimage.write_bytes(content)
    manager = linux.UpdateManager(bundle=str(appimage), method="appimage")
    monkeypatch.setattr(linux, "__version__", current)
    manifest = {"schema": 1, "version": available,
                "url": "https://example.invalid/FusedRender.AppImage",
                "sha256": "s", "signature": "g"}
    monkeypatch.setattr(common, "fetch_manifest", lambda url, **kwargs: dict(manifest))
    monkeypatch.setattr(manager, "_check_disk_space", lambda updates: None)
    manager.check()
    assert manager.status()["state"] == "available"
    return manager, appimage


def _row():
    rows = jobs.list_jobs()
    assert len(rows) == 1, rows
    return rows[0]


def test_updates_dir_is_the_appimages_own_parent(tmp_path):
    appimage = tmp_path / "sub" / "FusedRender.AppImage"
    appimage.parent.mkdir()
    appimage.write_bytes(b"x")
    manager = linux.UpdateManager(bundle=str(appimage), method="appimage")
    assert manager._updates_dir() == str(appimage.parent)


def test_sweep_spares_the_running_appimage_and_an_unrelated_sibling(tmp_path):
    # `_updates_dir()` on Linux IS the AppImage's own parent — a directory
    # the user owns, not a dedicated one this app controls — so the sweep
    # must touch only what a stale download of its own could plausibly have
    # left behind, never the whole directory. The released AppImage's own
    # name ("FusedRender-<version>-x86_64.AppImage") matches
    # `_DOWNLOAD_PREFIX`/`_DOWNLOAD_SUFFIX` just as well as a stale download
    # would, so name-matching alone is not enough: the manager's own bundle
    # path has to be excluded explicitly.
    parent = tmp_path / "Applications"
    parent.mkdir()
    appimage = parent / "FusedRender-9.9.9-x86_64.AppImage"
    appimage.write_bytes(b"the-running-appimage")
    sibling = parent / "some-users-unrelated-file.txt"
    sibling.write_bytes(b"not ours")
    stale = parent / "FusedRender-staged.AppImage"
    stale.write_bytes(b"a leftover from a previous session")

    manager = linux.UpdateManager(bundle=str(appimage), method="appimage")
    manager._sweep_stale_downloads()

    assert appimage.read_bytes() == b"the-running-appimage"
    assert sibling.read_bytes() == b"not ours"
    assert not stale.exists()  # the one thing actually worth sweeping


def test_install_refuses_when_the_parent_dir_is_not_writable(monkeypatch, tmp_path):
    manager, appimage = _appimage_manager(monkeypatch, tmp_path)
    monkeypatch.setattr(os, "access", lambda path, mode: False)
    manager.install()
    manager._install_thread.join(timeout=5)
    status = manager.status()
    assert status["state"] == "error"
    assert "cannot write to" in status["error"]
    # Nothing touched: the original AppImage is exactly what it was.
    assert appimage.read_bytes() == b"old-bytes"


def _sha256_hex(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def test_a_successful_install_swaps_the_appimage_and_writes_the_stamp(monkeypatch, tmp_path):
    new_bytes = b"new-appimage-bytes"

    def fake_download(manifest, *, dir, prefix, suffix, progress, should_abort):
        assert prefix == "FusedRender-"
        assert suffix == ".AppImage"
        progress(len(new_bytes), len(new_bytes))
        path = os.path.join(dir, prefix + "staged" + suffix)
        with open(path, "wb") as f:
            f.write(new_bytes)
        return path

    manager, appimage = _appimage_manager(monkeypatch, tmp_path)
    monkeypatch.setattr(common, "download_verified", fake_download)
    manager.install()
    manager._install_thread.join(timeout=5)

    assert appimage.read_bytes() == new_bytes
    # os.chmod'd executable.
    assert os.access(str(appimage), os.X_OK)
    status = manager.status()
    assert status["state"] == "installed"
    row = _row()
    assert row["state"] == "done"

    disk_version = manager._disk_version()
    assert disk_version == "9.9.9"


def test_install_resolves_a_symlinked_appimage_before_swapping(monkeypatch, tmp_path):
    # `$APPIMAGE`/`startup.appimage_path()` could in principle name a symlink
    # (today's type-2 runtime happens to resolve `/proc/self/exe` itself, so
    # it never does — but the swap must not depend on that). Swapping onto
    # the LINK's own path would replace the link with a plain file and
    # orphan the real artifact it used to point at; resolving first must
    # land the new bytes on the REAL file and leave the link resolving to it.
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real = real_dir / "FusedRender-0.4.10-x86_64.AppImage"
    real.write_bytes(b"old-bytes")
    link = tmp_path / "FusedRender.AppImage"
    link.symlink_to(real)

    manager = linux.UpdateManager(bundle=str(link), method="appimage")
    monkeypatch.setattr(linux, "__version__", "0.4.10")
    manifest = {"schema": 1, "version": "9.9.9",
                "url": "https://example.invalid/FusedRender.AppImage",
                "sha256": "s", "signature": "g"}
    monkeypatch.setattr(common, "fetch_manifest", lambda url, **kwargs: dict(manifest))
    monkeypatch.setattr(manager, "_check_disk_space", lambda updates: None)
    manager.check()
    assert manager.status()["state"] == "available"

    new_bytes = b"new-appimage-bytes"

    def fake_download(manifest, *, dir, prefix, suffix, progress, should_abort):
        # The download must land next to the REAL file, not the link — the
        # swap is only atomic within one filesystem.
        assert dir == str(real_dir)
        progress(len(new_bytes), len(new_bytes))
        path = os.path.join(dir, prefix + "staged" + suffix)
        with open(path, "wb") as f:
            f.write(new_bytes)
        return path

    monkeypatch.setattr(common, "download_verified", fake_download)
    manager.install()
    manager._install_thread.join(timeout=5)

    assert manager.status()["state"] == "installed"
    assert real.read_bytes() == new_bytes
    assert link.is_symlink()
    assert os.readlink(str(link)) == str(real)


def test_disk_version_reads_the_stamp_back(monkeypatch, tmp_path):
    appimage = tmp_path / "FusedRender.AppImage"
    appimage.write_bytes(b"payload")
    manager = linux.UpdateManager(bundle=str(appimage), method="appimage")
    assert manager._disk_version() is None
    linux.installed.write_linux_stamp(str(appimage), "1.2.3")
    assert manager._disk_version() == "1.2.3"
    # Moving/replacing the file invalidates the stamp (size/mtime drift).
    appimage.write_bytes(b"a different, longer payload")
    assert manager._disk_version() is None


def test_cancelling_mid_download_reverts_to_available_and_discards_the_partial(
        monkeypatch, tmp_path):
    manager, appimage = _appimage_manager(monkeypatch, tmp_path)

    def fake_download(manifest, *, dir, prefix, suffix, progress, should_abort):
        # progress() reports a tick and reads a cancel back off the reply —
        # the same round trip the real download loop does per chunk.
        jobs.request_cancel("sys:update:9.9.9")
        progress(4, 8)
        assert should_abort()
        raise common.UpdateCancelled("cancelled")

    monkeypatch.setattr(common, "download_verified", fake_download)
    manager.install()
    manager._install_thread.join(timeout=5)

    status = manager.status()
    assert status["state"] == "available"
    assert status["latest_version"] == "9.9.9"
    row = _row()
    assert row["state"] == "cancelled"
    # The original AppImage is untouched.
    assert appimage.read_bytes() == b"old-bytes"


def test_a_checksum_mismatch_leaves_the_original_appimage_untouched(monkeypatch, tmp_path):
    manager, appimage = _appimage_manager(monkeypatch, tmp_path)

    def bad_download(manifest, *, dir, prefix, suffix, progress, should_abort):
        raise ValueError("downloaded file does not match the signed manifest")

    monkeypatch.setattr(common, "download_verified", bad_download)
    manager.install()
    manager._install_thread.join(timeout=5)

    status = manager.status()
    assert status["state"] == "error"
    assert appimage.read_bytes() == b"old-bytes"
    row = _row()
    assert row["state"] == "error"
