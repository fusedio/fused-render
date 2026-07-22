"""Mount-safety of the fs mutation handlers and /api/fs/raw
(fused_render/server.py).

An rclone-backed NFS mount has no cheap point lookup: a cold negative kernel
probe (os.stat / os.path.exists / os.path.isdir / os.listdir) forces rclone to
enumerate the ENTIRE parent S3 prefix, blows past the macOS NFS deadman, and
DROPS the mount — server threads then block uninterruptibly. So no kernel FS
call may ever touch a mount-backed path in these handlers.

These tests pin:
  * the five fs mutation handlers (_fs_write/_fs_mkdir/_fs_delete/_fs_rename/
    _fs_copy) gate a mount path on read-only-ness BEFORE any kernel probe, and
    route existence/shape probes for a writable mount through the rclone rcd
    (rc_list_dir), never the kernel;
  * directory-tree operations on a mount (delete of a mount dir, rename/copy
    where a mount side is a directory) are refused rather than kernel-walked.

/api/fs/raw's HEAD + serve-loss mount-safety lives in test_server_fs_raw_mount_safe.

Every "must not touch the mount through the kernel" assertion is enforced with
a guard that makes any kernel FS probe under the mountpoint raise
AssertionError — a sneaked-in kernel call fails the test loudly.

Real rclone is never invoked: the StubRcd from test_shell_mounts answers the rc
calls (or rc_list_dir is monkeypatched directly), and FUSED_RENDER_HOME is
redirected per test.
"""

import json
import os

import pytest
from _mount_safe_helpers import (  # noqa: F401 — `home` is a reused fixture
    _entry,
    _list_raises,
    _list_returns,
    _mount,
    _no_kernel_on_mount,
    home,
)
from fastapi.responses import JSONResponse

import fused_render.shell.mounts as mounts_mod
from fused_render.server import _fs_copy as COPY
from fused_render.server import _fs_delete as DELETE
from fused_render.server import _fs_mkdir as MKDIR
from fused_render.server import _fs_rename as RENAME
from fused_render.server import _fs_stat as STAT
from fused_render.server import _fs_write as WRITE

# os.access always says yes for root, so the chmod-based gates can't trip.
skip_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="read-only bits are ignored when running as root",
)


# --------------------------------------------------------------------------- helpers


def _status(resp) -> int:
    return resp.status_code if isinstance(resp, JSONResponse) else 200


def _data(resp) -> dict:
    if isinstance(resp, JSONResponse):
        return json.loads(bytes(resp.body))
    return resp


# ===========================================================================
# Task 1 — early mount gate in the fs mutation handlers
# ===========================================================================

# -- _fs_write --------------------------------------------------------------


def test_write_read_only_mount_refused_before_any_kernel_probe(home, monkeypatch):
    mp = _mount("ro", read_only=True)
    _no_kernel_on_mount(monkeypatch, mp)
    # rc_list_dir must NOT be consulted either — readonly settles it first.
    _list_raises(monkeypatch, AssertionError("rc_list_dir called before readonly gate"))
    resp = WRITE({"path": os.path.join(mp, "notes.txt"), "content": "x"}, x_fused="1")
    assert _status(resp) == 403
    assert _data(resp)["error"] == "readonly"


def test_write_writable_mount_missing_parent_404_via_rc(home, monkeypatch):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    # Parent is not a listable directory (rc rejects the listing).
    _list_raises(monkeypatch, mounts_mod.RcListError("not a directory"))
    resp = WRITE({"path": os.path.join(mp, "sub", "notes.txt"), "content": "x"}, x_fused="1")
    assert _status(resp) == 404


def test_write_writable_mount_target_is_dir_400_via_rc(home, monkeypatch):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_returns(monkeypatch, [_entry("notes.txt", is_dir=True)])
    resp = WRITE({"path": os.path.join(mp, "notes.txt"), "content": "x"}, x_fused="1")
    assert _status(resp) == 400
    assert "directory" in _data(resp)["error"]


def test_write_writable_mount_indeterminate_is_503_not_missing(home, monkeypatch):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_raises(monkeypatch, mounts_mod.RcListUnavailable("rcd down"))
    resp = WRITE({"path": os.path.join(mp, "notes.txt"), "content": "x"}, x_fused="1")
    assert _status(resp) == 503


def test_write_writable_mount_create_conflict_409_via_rc(home, monkeypatch):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_returns(monkeypatch, [_entry("notes.txt", size=3)])
    resp = WRITE(
        {"path": os.path.join(mp, "notes.txt"), "content": "x", "create": True}, x_fused="1"
    )
    assert _status(resp) == 409
    assert _data(resp)["error"] == "conflict"


def test_write_mount_subsecond_mtime_gap_does_not_conflict(home, monkeypatch):
    # The client's expected_mtime is a kernel /api/fs/stat st_mtime; the mount
    # conflict check compares it against the rc ModTime. The two sources
    # disagree sub-second, so the MOUNT branch tolerates < 1s and must NOT 409.
    mp = _mount("rw", read_only=False, on_disk=True)
    _no_kernel_on_mount(monkeypatch, mp)
    modtime = "2024-01-02T03:04:05Z"
    _list_returns(monkeypatch, [_entry("notes.txt", size=3, mtime=modtime)])
    epoch = mounts_mod.rc_modtime_epoch(modtime)
    resp = WRITE(
        {"path": os.path.join(mp, "notes.txt"), "content": "x", "expected_mtime": epoch + 0.4},
        x_fused="1",
    )
    assert _status(resp) == 200


def test_write_mount_large_mtime_gap_still_conflicts(home, monkeypatch):
    # A gap beyond the cross-source tolerance is a genuine concurrent change:
    # still a 409, so the widened tolerance can't mask a real conflict.
    mp = _mount("rw", read_only=False, on_disk=True)
    _no_kernel_on_mount(monkeypatch, mp)
    modtime = "2024-01-02T03:04:05Z"
    _list_returns(monkeypatch, [_entry("notes.txt", size=3, mtime=modtime)])
    epoch = mounts_mod.rc_modtime_epoch(modtime)
    resp = WRITE(
        {"path": os.path.join(mp, "notes.txt"), "content": "x", "expected_mtime": epoch + 5.0},
        x_fused="1",
    )
    assert _status(resp) == 409
    assert _data(resp)["error"] == "conflict"


def test_write_writable_mount_new_file_succeeds_via_vfs(home, monkeypatch):
    mp = _mount("rw", read_only=False, on_disk=True)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_returns(monkeypatch, [])  # parent listable, target absent
    target = os.path.join(mp, "notes.txt")
    resp = WRITE({"path": target, "content": "hello"}, x_fused="1")
    assert _status(resp) == 200
    out = _data(resp)
    assert out["writable"] is True and out["remote"] is True and out["is_dir"] is False
    # The bytes actually landed (the mutation goes through the VFS).
    with open(target) as fh:
        assert fh.read() == "hello"


# -- _fs_mkdir --------------------------------------------------------------


def test_mkdir_read_only_mount_refused_before_probe(home, monkeypatch):
    mp = _mount("ro", read_only=True)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_raises(monkeypatch, AssertionError("rc_list_dir called before readonly gate"))
    resp = MKDIR({"path": os.path.join(mp, "newdir")}, x_fused="1")
    assert _status(resp) == 403
    assert _data(resp)["error"] == "readonly"


def test_mkdir_writable_mount_conflict_409_via_rc(home, monkeypatch):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_returns(monkeypatch, [_entry("newdir", is_dir=True)])
    resp = MKDIR({"path": os.path.join(mp, "newdir")}, x_fused="1")
    assert _status(resp) == 409
    assert _data(resp)["error"] == "conflict"


def test_mkdir_writable_mount_indeterminate_503(home, monkeypatch):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_raises(monkeypatch, mounts_mod.RcListTimeout("too many entries"))
    resp = MKDIR({"path": os.path.join(mp, "newdir")}, x_fused="1")
    assert _status(resp) == 503


def test_mkdir_writable_mount_success_via_vfs(home, monkeypatch):
    # The DECISION phase (readonly gate + existence probe) is proven kernel-free
    # by the refusal/missing/indeterminate tests; here the guard is off so the
    # VFS mutation itself (os.mkdir) can land, per the task's "mutation is OK".
    mp = _mount("rw", read_only=False, on_disk=True)
    _list_returns(monkeypatch, [])  # parent listable, target absent
    target = os.path.join(mp, "newdir")
    resp = MKDIR({"path": target}, x_fused="1")
    assert _status(resp) == 200
    out = _data(resp)
    assert out["is_dir"] is True and out["writable"] is True and out["remote"] is True
    assert os.path.isdir(target)


# -- _fs_delete -------------------------------------------------------------


def test_delete_read_only_mount_refused_before_probe(home, monkeypatch):
    mp = _mount("ro", read_only=True)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_raises(monkeypatch, AssertionError("rc_list_dir called before readonly gate"))
    resp = DELETE({"path": os.path.join(mp, "notes.txt")}, x_fused="1")
    assert _status(resp) == 403
    assert _data(resp)["error"] == "readonly"


def test_delete_mount_directory_refused_not_kernel_walked(home, monkeypatch):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    # rc reports the target is a directory; a real delete would os.listdir /
    # shutil.rmtree over the remote tree — refuse instead.
    _list_returns(monkeypatch, [_entry("adir", is_dir=True)])
    resp = DELETE({"path": os.path.join(mp, "adir")}, x_fused="1")
    assert _status(resp) == 400
    assert "director" in _data(resp)["error"].lower()


def test_delete_mount_directory_recursive_still_refused(home, monkeypatch):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_returns(monkeypatch, [_entry("adir", is_dir=True)])
    resp = DELETE({"path": os.path.join(mp, "adir"), "recursive": True}, x_fused="1")
    assert _status(resp) == 400


def test_delete_mount_missing_404_via_rc(home, monkeypatch):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_returns(monkeypatch, [_entry("other.txt")])  # target absent
    resp = DELETE({"path": os.path.join(mp, "gone.txt")}, x_fused="1")
    assert _status(resp) == 404


def test_delete_mount_indeterminate_503(home, monkeypatch):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_raises(monkeypatch, mounts_mod.RcListUnavailable("rcd down"))
    resp = DELETE({"path": os.path.join(mp, "notes.txt")}, x_fused="1")
    assert _status(resp) == 503


def test_delete_mount_single_file_succeeds_via_vfs(home, monkeypatch):
    # Guard off: the decision phase is pinned kernel-free elsewhere; here the
    # VFS unlink (os.remove) must be free to run.
    mp = _mount("rw", read_only=False, on_disk=True)
    target = os.path.join(mp, "notes.txt")
    with open(target, "w") as fh:
        fh.write("bytes")
    _list_returns(monkeypatch, [_entry("notes.txt", size=5)])
    resp = DELETE({"path": target}, x_fused="1")
    assert _status(resp) == 200
    assert _data(resp)["deleted"] == target
    assert not os.path.exists(target)


# -- _fs_rename -------------------------------------------------------------


def test_rename_read_only_mount_src_refused(home, monkeypatch):
    mp = _mount("ro", read_only=True)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_raises(monkeypatch, AssertionError("probed before readonly gate"))
    resp = RENAME({"src": os.path.join(mp, "a.txt"), "dst": os.path.join(mp, "b.txt")}, x_fused="1")
    assert _status(resp) == 403
    assert _data(resp)["error"] == "readonly"


def test_rename_mount_directory_src_refused(home, monkeypatch):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_returns(monkeypatch, [_entry("adir", is_dir=True), _entry("b", is_dir=True)])
    resp = RENAME(
        {"src": os.path.join(mp, "adir"), "dst": os.path.join(mp, "b", "adir")}, x_fused="1"
    )
    assert _status(resp) == 400
    assert "director" in _data(resp)["error"].lower()


def test_rename_mount_missing_src_404_via_rc(home, monkeypatch):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_returns(monkeypatch, [_entry("other.txt")])  # src absent
    resp = RENAME(
        {"src": os.path.join(mp, "gone.txt"), "dst": os.path.join(mp, "b.txt")}, x_fused="1"
    )
    assert _status(resp) == 404


def test_rename_mount_single_file_succeeds(home, monkeypatch):
    # Guard off: the decision phase is pinned kernel-free elsewhere; here the
    # VFS move (shutil.move, which probes/writes dst) must be free to run.
    mp = _mount("rw", read_only=False, on_disk=True)
    src = os.path.join(mp, "a.txt")
    with open(src, "w") as fh:
        fh.write("data")
    _list_returns(monkeypatch, [_entry("a.txt", size=4)])
    dst = os.path.join(mp, "b.txt")
    resp = RENAME({"src": src, "dst": dst}, x_fused="1")
    assert _status(resp) == 200
    assert os.path.exists(dst) and not os.path.exists(src)


# -- _fs_copy ---------------------------------------------------------------


def test_copy_read_only_mount_dst_refused(home, monkeypatch, tmp_path):
    mp = _mount("ro", read_only=True)
    local_src = tmp_path / "src.txt"
    local_src.write_text("x")
    _no_kernel_on_mount(monkeypatch, mp)
    _list_raises(monkeypatch, AssertionError("probed before readonly gate"))
    resp = COPY({"src": str(local_src), "dst": os.path.join(mp, "dst.txt")}, x_fused="1")
    assert _status(resp) == 403
    assert _data(resp)["error"] == "readonly"


def test_copy_mount_directory_src_refused(home, monkeypatch, tmp_path):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_returns(monkeypatch, [_entry("adir", is_dir=True)])
    resp = COPY({"src": os.path.join(mp, "adir"), "dst": str(tmp_path / "out")}, x_fused="1")
    assert _status(resp) == 400
    assert "director" in _data(resp)["error"].lower()


def test_copy_mount_missing_src_404_via_rc(home, monkeypatch, tmp_path):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_returns(monkeypatch, [_entry("other.txt")])
    resp = COPY(
        {"src": os.path.join(mp, "gone.txt"), "dst": str(tmp_path / "out.txt")}, x_fused="1"
    )
    assert _status(resp) == 404


def test_copy_mount_indeterminate_503(home, monkeypatch, tmp_path):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_raises(monkeypatch, mounts_mod.RcListTimeout("too big"))
    resp = COPY({"src": os.path.join(mp, "a.txt"), "dst": str(tmp_path / "out.txt")}, x_fused="1")
    assert _status(resp) == 503


# ===========================================================================
# Mounts-root children exist only when a mount RECORD carries the name
# (Bugbot 3615342568 "Mount root children always exist"). A direct child of the
# mounts container is a mountpoint iff mounts.json lists its name — an unknown/
# removed name is a phantom that must read as ABSENT, settled from mounts.json
# with no rc_list_dir / no I/O on any mount.
# ===========================================================================


def test_mounts_root_unknown_child_reads_as_absent_delete_404(home, monkeypatch):
    # An unknown name directly under the mounts container has no mount record:
    # DELETE must 404 (absent), NOT treat it as an existing mountpoint dir. The
    # rc listing must never be consulted — mounts.json settles it.
    _list_raises(monkeypatch, AssertionError("rc_list_dir consulted for a mounts-root child"))
    resp = DELETE({"path": os.path.join(mounts_mod.mounts_dir(), "phantom")}, x_fused="1")
    assert _status(resp) == 404


def test_mounts_root_unknown_child_mkdir_not_conflict(home, monkeypatch):
    # MKDIR of an unknown mounts-root name must NOT 409: before the fix every
    # basename read as "already exists". With the phantom absent, the create
    # proceeds (200).
    _list_raises(monkeypatch, AssertionError("rc_list_dir consulted for a mounts-root child"))
    resp = MKDIR({"path": os.path.join(mounts_mod.mounts_dir(), "phantom")}, x_fused="1")
    assert _status(resp) != 409


def test_stat_mounts_root_is_local_dir_not_indeterminate_503(home, monkeypatch):
    # /api/fs/stat on the mounts CONTAINER itself must return the local dir's
    # stat (200, is_dir=True), NOT route through rc_stat_result — the container
    # has no single mount record, so the rc stat is indeterminate and _fs_stat
    # would map it to a spurious 503 "mount is slow or unresponsive". The rc
    # stat path must never be consulted for the root.
    def _boom(*a, **k):
        raise AssertionError("rc_stat_result consulted for the mounts root")

    monkeypatch.setattr(mounts_mod, "rc_stat_result", _boom)
    resp = STAT(mounts_mod.mounts_dir())
    assert _status(resp) == 200
    out = _data(resp)
    assert out["is_dir"] is True
    assert out["path"] == mounts_mod.mounts_dir()


def test_stat_symlink_to_mounts_root_treated_as_root_not_503(home, monkeypatch, tmp_path):
    # A symlink whose TARGET is the mounts container root is is_mount_backed
    # (via its realpath branch), so it must ALSO read as is_mounts_root — else
    # the _mount_safe_stat guard `is_mount_backed and not is_mounts_root` sends
    # it through rc_stat_result, which finds no mount record for the container
    # and surfaces the exact spurious 503 the root guard exists to prevent.
    link = tmp_path / "mounts-link"
    os.symlink(mounts_mod.mounts_dir(), link)
    # is_mounts_root must resolve the symlink TO the root (mirrors is_mount_backed).
    assert mounts_mod.is_mounts_root(str(link)) is True

    def _boom(*a, **k):
        raise AssertionError("rc_stat_result consulted for a symlink to the mounts root")

    monkeypatch.setattr(mounts_mod, "rc_stat_result", _boom)
    resp = STAT(str(link))
    assert _status(resp) == 200
    out = _data(resp)
    assert out["is_dir"] is True


def test_mounts_root_known_mount_name_still_exists_mkdir_409(home, monkeypatch):
    # A name carried by a real mount record still reads as an existing dir, so
    # MKDIR over it is a 409 conflict (the fix must not regress this direction).
    mp = _mount("real", read_only=False)
    _list_raises(monkeypatch, AssertionError("rc_list_dir consulted for a mounts-root child"))
    resp = MKDIR({"path": mp}, x_fused="1")
    assert _status(resp) == 409
    assert _data(resp)["error"] == "conflict"


# ===========================================================================
# Local side of a mixed local+mount rename/copy still gets the _writable check
# (Bugbot 3615342580 "Local writable checks skipped"). The mount read-only gate
# only covers the mount side; a chmod-protected LOCAL src (rename) or a
# non-writable LOCAL dst must still 403 "readonly", same as the all-local branch.
# The mount side is NEVER _writable-probed (that kernel-stats a writable mount).
# ===========================================================================


@skip_root
def test_rename_local_readonly_src_refused_with_mount_dst(home, monkeypatch, tmp_path):
    mp = _mount("rw", read_only=False, on_disk=True)
    _no_kernel_on_mount(monkeypatch, mp)
    src = tmp_path / "src.txt"
    src.write_text("data")
    os.chmod(src, 0o400)  # read-only local file: a move (delete+write) must 403
    _list_returns(monkeypatch, [])  # mount dst parent listable, dst absent
    try:
        resp = RENAME({"src": str(src), "dst": os.path.join(mp, "b.txt")}, x_fused="1")
        assert _status(resp) == 403
        assert _data(resp)["error"] == "readonly"
    finally:
        os.chmod(src, 0o600)


@skip_root
def test_copy_local_readonly_dst_refused_with_mount_src(home, monkeypatch, tmp_path):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    ro_dir = tmp_path / "roout"
    ro_dir.mkdir()
    os.chmod(ro_dir, 0o500)  # non-writable local dst parent -> new file refused
    _list_returns(monkeypatch, [_entry("a.txt", size=4)])  # mount src is a file
    try:
        resp = COPY({"src": os.path.join(mp, "a.txt"), "dst": str(ro_dir / "b.txt")}, x_fused="1")
        assert _status(resp) == 403
        assert _data(resp)["error"] == "readonly"
    finally:
        os.chmod(ro_dir, 0o700)
