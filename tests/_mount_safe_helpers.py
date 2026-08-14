"""Shared fixtures + helpers for the mount-safety tests
(test_server_fs_mount_safe, test_server_fs_raw_mount_safe).

Kept in a non-test module (not a test_* file) so both suites import from a
neutral home instead of one test module reaching into another. The `home`
fixture is imported into each suite's namespace and used like a local fixture.

Real rclone is never invoked: rc_list_dir is monkeypatched directly and
FUSED_RENDER_HOME is redirected per test.
"""
import os

import pytest

import fused_render.shell.mounts as mounts_mod


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
