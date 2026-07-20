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
from fastapi.responses import JSONResponse

import fused_render.shell.mounts as mounts_mod
from fused_render.server import _fs_copy as COPY
from fused_render.server import _fs_delete as DELETE
from fused_render.server import _fs_mkdir as MKDIR
from fused_render.server import _fs_rename as RENAME
from fused_render.server import _fs_write as WRITE


# --------------------------------------------------------------------------- helpers

def _status(resp) -> int:
    return resp.status_code if isinstance(resp, JSONResponse) else 200


def _data(resp) -> dict:
    if isinstance(resp, JSONResponse):
        return json.loads(bytes(resp.body))
    return resp


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "mounts").mkdir(parents=True)
    monkeypatch.setenv("FUSED_RENDER_HOME", str(h))
    # add_mount before create_app would otherwise spawn the automount thread.
    monkeypatch.setattr(mounts_mod, "startup", lambda: None)
    return h


def _mount(name, read_only=False, on_disk=False):
    """Create a mount record and return its mountpoint. on_disk makes the
    mountpoint a real local directory (so a VFS-style write can land) — the
    mount is fake, there is no real NFS underneath."""
    c = mounts_mod.add_mount(name, f"{name}-remote:bucket", read_only=read_only)
    mp = mounts_mod.mountpoint(c)
    if on_disk:
        os.makedirs(mp, exist_ok=True)
    return mp


def _entry(name, is_dir=False, size=0, mtime="2024-01-02T03:04:05Z"):
    return {"Name": name, "IsDir": is_dir,
            "Size": -1 if is_dir else size, "ModTime": mtime}


def _no_kernel_on_mount(monkeypatch, mp):
    """Make any kernel FS probe under `mp` raise AssertionError. Proves the
    handler answered existence/shape via the rclone rcd, never the kernel.

    The `mount-probe-*` background thread is exempt: broken_mount_error (in
    mounts.py, off-limits here) spawns it to kernel-probe the mountpoint OFF the
    request path when mapping an indeterminate listing to 503 — that is not the
    synchronous decision probe these tests police."""
    import threading

    real_os = {n: getattr(os, n) for n in ("stat", "lstat", "listdir", "scandir")}
    real_path = {n: getattr(os.path, n) for n in ("exists", "isdir", "islink")}

    def _wrap(fn, name):
        def guarded(path, *a, **k):
            try:
                p = os.fspath(path)
            except TypeError:
                p = path
            if (isinstance(p, str) and (p == mp or p.startswith(mp + os.sep))
                    and not threading.current_thread().name.startswith("mount-probe")):
                raise AssertionError(f"kernel {name}({p}) touched the mount")
            return fn(path, *a, **k)
        return guarded

    for n, fn in real_os.items():
        monkeypatch.setattr(os, n, _wrap(fn, "os." + n))
    for n, fn in real_path.items():
        monkeypatch.setattr(os.path, n, _wrap(fn, "os.path." + n))


def _list_returns(monkeypatch, entries):
    monkeypatch.setattr(mounts_mod, "rc_list_dir",
                        lambda p, timeout=None: list(entries))


def _list_raises(monkeypatch, exc):
    def boom(p, timeout=None):
        raise exc
    monkeypatch.setattr(mounts_mod, "rc_list_dir", boom)


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
    resp = WRITE({"path": os.path.join(mp, "sub", "notes.txt"), "content": "x"},
                 x_fused="1")
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
    resp = WRITE({"path": os.path.join(mp, "notes.txt"), "content": "x", "create": True},
                 x_fused="1")
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
    resp = RENAME({"src": os.path.join(mp, "a.txt"),
                   "dst": os.path.join(mp, "b.txt")}, x_fused="1")
    assert _status(resp) == 403
    assert _data(resp)["error"] == "readonly"


def test_rename_mount_directory_src_refused(home, monkeypatch):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_returns(monkeypatch, [_entry("adir", is_dir=True), _entry("b", is_dir=True)])
    resp = RENAME({"src": os.path.join(mp, "adir"),
                   "dst": os.path.join(mp, "b", "adir")}, x_fused="1")
    assert _status(resp) == 400
    assert "director" in _data(resp)["error"].lower()


def test_rename_mount_missing_src_404_via_rc(home, monkeypatch):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_returns(monkeypatch, [_entry("other.txt")])  # src absent
    resp = RENAME({"src": os.path.join(mp, "gone.txt"),
                   "dst": os.path.join(mp, "b.txt")}, x_fused="1")
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
    resp = COPY({"src": str(local_src), "dst": os.path.join(mp, "dst.txt")},
                x_fused="1")
    assert _status(resp) == 403
    assert _data(resp)["error"] == "readonly"


def test_copy_mount_directory_src_refused(home, monkeypatch, tmp_path):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_returns(monkeypatch, [_entry("adir", is_dir=True)])
    resp = COPY({"src": os.path.join(mp, "adir"), "dst": str(tmp_path / "out")},
                x_fused="1")
    assert _status(resp) == 400
    assert "director" in _data(resp)["error"].lower()


def test_copy_mount_missing_src_404_via_rc(home, monkeypatch, tmp_path):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_returns(monkeypatch, [_entry("other.txt")])
    resp = COPY({"src": os.path.join(mp, "gone.txt"), "dst": str(tmp_path / "out.txt")},
                x_fused="1")
    assert _status(resp) == 404


def test_copy_mount_indeterminate_503(home, monkeypatch, tmp_path):
    mp = _mount("rw", read_only=False)
    _no_kernel_on_mount(monkeypatch, mp)
    _list_raises(monkeypatch, mounts_mod.RcListTimeout("too big"))
    resp = COPY({"src": os.path.join(mp, "a.txt"), "dst": str(tmp_path / "out.txt")},
                x_fused="1")
    assert _status(resp) == 503

