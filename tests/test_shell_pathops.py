"""Unit tests for fused_render.shell.pathops — the mount-aware path-operation
facade that centralizes the direct→rc listing ladder and the mount-safe
file-existence probe.

These tests drive pathops in isolation by monkeypatching the mounts-module
functions it dispatches through (looked up on the module at CALL time, exactly
as the server's own tests patch them). Two invariants are load-bearing and get
their own guards:
  * the direct→rc dispatch/fallback picks the right route and preserves the
    resume token;
  * pathops NEVER issues a kernel os.scandir/os.stat on a mount-backed path
    (the NFS-wedge class the whole mount layer exists to avoid).
"""
import os

import pytest

from fused_render.shell import mounts as mounts_mod
from fused_render.shell import pathops


# A mount path is any absolute path; the tests always stub is_mount_backed so no
# real mounts dir is needed. This is a plausible mountpoint-relative path.
MOUNT_PATH = "/home/u/.fused-render/mounts/open/some/dir"


def _entry(name, *, is_dir=False, size=1, modtime="2024-01-02T03:04:05Z"):
    return {"Name": name, "IsDir": is_dir, "Size": size, "ModTime": modtime}


@pytest.fixture
def as_mount(monkeypatch):
    """Treat MOUNT_PATH (and only it) as mount-backed, with NO kernel I/O."""
    monkeypatch.setattr(mounts_mod, "is_mount_backed",
                        lambda p: p == MOUNT_PATH)
    return MOUNT_PATH


@pytest.fixture
def no_kernel_fs(monkeypatch):
    """Make any kernel directory/stat syscall explode, so a test can prove a
    mount-backed path never reaches the kernel (the mount-wedge guarantee)."""
    def boom(*a, **k):
        raise AssertionError(f"kernel fs call on a mount path: {a!r}")

    monkeypatch.setattr(os, "scandir", boom)
    monkeypatch.setattr(os, "stat", boom)
    monkeypatch.setattr(os, "lstat", boom)


# --------------------------------------------------------------- list_mount_dir


def test_list_direct_route_returns_entries_and_token(as_mount, monkeypatch):
    monkeypatch.setattr(mounts_mod, "direct_list_capable", lambda p: True)

    def fake_page(path, *, max_keys, continuation=None, timeout=None):
        return [_entry("a.txt"), _entry("b")], "NEXT"

    monkeypatch.setattr(mounts_mod, "direct_list_page", fake_page)
    monkeypatch.setattr(mounts_mod, "rc_list_dir",
                        lambda *a, **k: pytest.fail("rc used on direct route"))

    # max_entries=2 so the first (2-entry) page fills the cap and the loop stops
    # with the resume token still set — the partial-listing shape.
    listing = pathops.list_mount_dir(as_mount, max_entries=2)
    assert listing.direct is True
    assert listing.token == "NEXT"
    assert [e["Name"] for e in listing.entries] == ["a.txt", "b"]


def test_list_falls_back_to_rc_on_direct_error(as_mount, monkeypatch):
    monkeypatch.setattr(mounts_mod, "direct_list_capable", lambda p: True)

    def boom_page(path, *, max_keys, continuation=None, timeout=None):
        raise mounts_mod.DirectListError("kaboom")

    monkeypatch.setattr(mounts_mod, "direct_list_page", boom_page)
    monkeypatch.setattr(mounts_mod, "rc_list_dir",
                        lambda p, timeout=None: [_entry("fromrc")])

    listing = pathops.list_mount_dir(as_mount, max_entries=10)
    assert listing.direct is False
    assert listing.token is None
    assert [e["Name"] for e in listing.entries] == ["fromrc"]


def test_list_no_rc_fallback_reraises_direct_error(as_mount, monkeypatch):
    monkeypatch.setattr(mounts_mod, "direct_list_capable", lambda p: True)

    def boom_page(path, *, max_keys, continuation=None, timeout=None):
        raise mounts_mod.DirectListError("kaboom")

    monkeypatch.setattr(mounts_mod, "direct_list_page", boom_page)
    monkeypatch.setattr(mounts_mod, "rc_list_dir",
                        lambda *a, **k: pytest.fail("rc must not run"))

    with pytest.raises(mounts_mod.DirectListError):
        pathops.list_mount_dir(as_mount, max_entries=10, allow_rc_fallback=False)


def test_list_non_direct_backend_uses_rc(as_mount, monkeypatch):
    monkeypatch.setattr(mounts_mod, "direct_list_capable", lambda p: False)
    monkeypatch.setattr(mounts_mod, "direct_list_page",
                        lambda *a, **k: pytest.fail("direct must not run"))
    seen = {}

    def fake_rc(p, timeout=None):
        seen["timeout"] = timeout
        return [_entry("x")]

    monkeypatch.setattr(mounts_mod, "rc_list_dir", fake_rc)

    listing = pathops.list_mount_dir(as_mount, max_entries=10, rc_timeout=7.0)
    assert listing.direct is False
    assert seen["timeout"] == 7.0
    assert [e["Name"] for e in listing.entries] == ["x"]


def test_list_rc_errors_propagate(as_mount, monkeypatch):
    monkeypatch.setattr(mounts_mod, "direct_list_capable", lambda p: False)

    def boom(p, timeout=None):
        raise mounts_mod.RcListTimeout("too big")

    monkeypatch.setattr(mounts_mod, "rc_list_dir", boom)
    with pytest.raises(mounts_mod.RcListTimeout):
        pathops.list_mount_dir(as_mount, max_entries=10)


def test_list_never_touches_kernel_on_mount(as_mount, no_kernel_fs, monkeypatch):
    monkeypatch.setattr(mounts_mod, "direct_list_capable", lambda p: False)
    monkeypatch.setattr(mounts_mod, "rc_list_dir",
                        lambda p, timeout=None: [_entry("x")])
    # Must complete without tripping the no_kernel_fs booby traps.
    listing = pathops.list_mount_dir(as_mount, max_entries=10)
    assert [e["Name"] for e in listing.entries] == ["x"]


def test_list_cap_never_overshoots(as_mount, monkeypatch):
    monkeypatch.setattr(mounts_mod, "direct_list_capable", lambda p: True)

    def fake_page(path, *, max_keys, continuation=None, timeout=None):
        # Honour max_keys so accumulation stops exactly at max_entries.
        return [_entry(f"f{i}") for i in range(max_keys)], "MORE"

    monkeypatch.setattr(mounts_mod, "direct_list_page", fake_page)
    listing = pathops.list_mount_dir(as_mount, max_entries=3)
    assert len(listing.entries) == 3
    assert listing.token == "MORE"  # partial → resumable


# ---------------------------------------------------------------- is_file / kind


@pytest.mark.parametrize("kind,expected", [
    ("file", True),
    ("indeterminate", True),  # fail OPEN: a transient rc hiccup keeps the file
    ("dir", False),
    ("missing", False),
])
def test_mount_is_file_fail_open(as_mount, monkeypatch, kind, expected):
    monkeypatch.setattr(mounts_mod, "rc_kind_for", lambda p, **k: kind)
    assert pathops.mount_is_file(as_mount) is expected


def test_is_file_routes_mount_through_rc(as_mount, no_kernel_fs, monkeypatch):
    monkeypatch.setattr(mounts_mod, "rc_kind_for", lambda p, **k: "file")
    # no_kernel_fs guarantees os.stat is never called for the mount path.
    assert pathops.is_file(as_mount) is True


def test_is_file_routes_local_through_kernel(tmp_path, monkeypatch):
    monkeypatch.setattr(mounts_mod, "is_mount_backed", lambda p: False)
    f = tmp_path / "hello.txt"
    f.write_text("hi")
    assert pathops.is_file(str(f)) is True
    assert pathops.is_file(str(tmp_path / "missing")) is False
    assert pathops.is_file(str(tmp_path)) is False  # a dir is not a file


def test_local_is_file(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("hi")
    assert pathops.local_is_file(str(f)) is True
    assert pathops.local_is_file(str(tmp_path / "nope")) is False
