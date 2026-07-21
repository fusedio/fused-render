"""Tests for the macOS Finder/Spotlight enumeration guards on the NFS mounts
(shell/mounts.py): the "nobrowse" NFS mount flag and the
`.metadata_never_index` Spotlight-exclusion marker.

Both exist to stop macOS from autonomously walking an S3-backed mount with
readdir (a prefix-enumeration mount-wedge trigger). No real mount is ever
created and mdutil is never invoked — FUSED_RENDER_HOME is redirected so the
marker lands under a tmp dir, and the mount flag is asserted on the option list
directly.
"""
import os

import pytest

import fused_render.shell.mounts as mounts_mod


@pytest.fixture()
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("FUSED_RENDER_HOME", str(home))
    return home


def test_nfs_mount_opt_includes_nobrowse():
    # "nobrowse" keeps the mount out of Finder and off Spotlight's auto-scan;
    # without it, browsing/indexing readdir's the S3-backed mount into a full
    # prefix enumeration. It rides the same ExtraOptions list as timeo/retrans
    # that rclone forwards verbatim to the macOS mount command.
    assert "nobrowse" in mounts_mod.NFS_MOUNT_OPT["ExtraOptions"]
    # Still carried through the per-mount option builder (alongside rdonly for
    # read-only mounts), which is what attach_mount actually passes to rcd.
    assert "nobrowse" in mounts_mod._nfs_mount_opt({"read_only": False})["ExtraOptions"]
    assert "nobrowse" in mounts_mod._nfs_mount_opt({"read_only": True})["ExtraOptions"]


def test_ensure_mounts_dir_drops_spotlight_marker(home):
    # Creating the mounts root must drop the `.metadata_never_index` marker so
    # Spotlight (mds) skips the whole subtree — the browse-side companion to the
    # nobrowse mount flag.
    root = mounts_mod.ensure_mounts_dir()
    assert root == mounts_mod.mounts_dir()
    assert os.path.isdir(root)
    assert os.path.isfile(os.path.join(root, ".metadata_never_index"))


def test_ensure_mounts_dir_is_idempotent(home):
    # Called on every attach; a second call must not raise or clobber.
    mounts_mod.ensure_mounts_dir()
    mounts_mod.ensure_mounts_dir()
    assert os.path.isfile(
        os.path.join(mounts_mod.mounts_dir(), ".metadata_never_index")
    )
