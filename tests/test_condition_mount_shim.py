"""The condition-gate mount shim (server._run_condition + _conditions_payload +
_fs_stat).

`/api/fs/conditions` runs template condition gates (`condition.py`) whose plain
`os.path.isfile`/`isdir`/`os.stat`/`open` hit the kernel NFS mount. A cold
NEGATIVE `os.path.isfile` on a mount forces rclone to LIST the whole parent S3
prefix (~18-24s) and trips the macOS NFS deadman -> the mount is dropped. For a
mount-backed target the gate runs under a per-call, thread-safe shim that routes
those primitives through the rclone rc API instead of the kernel (Solution A).

These tests drive the shim through the server surface with the rc helpers
monkeypatched (the helpers themselves are tested against a real stub rcd in
tests/test_shell_mounts.py), and GUARD the real kernel os.* so any leak fails
loudly — the mount-safety property is the whole point.
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

import fused_render.shell.mounts as mounts_mod
from fused_render import server

MOUNT_PREFIX = "/fake-mounts/"
STORE = "/fake-mounts/s3demo/store"
ZARR_CONDITION = os.path.join(server.TEMPLATES_DIR, "zarr_aoi", "condition.py")


@pytest.fixture()
def guard_kernel(monkeypatch):
    """Make every kernel os call on a mount-backed path explode, so a shim leak
    fails loudly. Non-mount paths (template files, tmp fixtures) pass through."""
    real = {
        "isfile": os.path.isfile,
        "isdir": os.path.isdir,
        "exists": os.path.exists,
        "stat": os.stat,
        "listdir": os.listdir,
        "scandir": os.scandir,
    }

    def _guard(name, fn):
        def wrapped(p, *a, **k):
            if isinstance(p, str) and p.startswith(MOUNT_PREFIX):
                raise AssertionError(f"kernel os.{name} on mount path {p!r}")
            return fn(p, *a, **k)
        return wrapped

    for name in ("isfile", "isdir", "exists"):
        monkeypatch.setattr(os.path, name, _guard(name, real[name]))
    for name in ("stat", "listdir", "scandir"):
        monkeypatch.setattr(os, name, _guard(name, real[name]))
    return real


def _mount(monkeypatch, kind_map, read_bytes=None):
    """Route the mount prefix through fake rc helpers. `kind_map` maps an exact
    path (or the string "*") to a rc_kind_for verdict; `read_bytes` is what
    rc_read_bounded returns for the zarr.json probe."""
    monkeypatch.setattr(mounts_mod, "is_mount_backed",
                        lambda p: isinstance(p, str) and p.startswith(MOUNT_PREFIX))

    def _kind(p):
        return kind_map.get(p, kind_map.get("*", "missing"))

    monkeypatch.setattr(mounts_mod, "rc_kind_for", _kind)

    def _read(p, *a, **k):
        if read_bytes is None:
            raise OSError("no serve")
        return read_bytes

    monkeypatch.setattr(mounts_mod, "rc_read_bounded", _read)


# ----------------------------------------------------------- _run_condition shim


def test_shimmed_gate_group_true_with_zero_kernel_os_calls(monkeypatch, guard_kernel):
    # A v3 group root on a mount: isdir(dir) hits, .zmetadata misses, zarr.json
    # hits, its node_type=="group" -> True. Every probe routes via rc; the kernel
    # guard proves not one os.* call touched the mount.
    _mount(
        monkeypatch,
        {STORE: "dir", STORE + "/zarr.json": "file"},
        read_bytes=b'{"node_type": "group"}',
    )
    allowed, err = server._run_condition(ZARR_CONDITION, STORE)
    assert allowed is True and err is None


def test_shimmed_gate_bare_array_is_false(monkeypatch, guard_kernel):
    # v3 bare-array root: zarr.json present but node_type=="array" -> not a group
    # -> False (fail-closed correctness preserved over the shim).
    _mount(
        monkeypatch,
        {STORE: "dir", STORE + "/zarr.json": "file"},
        read_bytes=b'{"node_type": "array"}',
    )
    allowed, err = server._run_condition(ZARR_CONDITION, STORE)
    assert allowed is False and err is None


def test_shimmed_gate_plain_dir_is_false(monkeypatch, guard_kernel):
    # A directory with no store markers: isdir hits, all three isfile probes miss
    # -> False, all off the kernel.
    _mount(monkeypatch, {STORE: "dir", "*": "missing"})
    allowed, err = server._run_condition(ZARR_CONDITION, STORE)
    assert allowed is False and err is None


def test_shimmed_gate_named_zarr_fast_path_true(monkeypatch, guard_kernel):
    # The zero-I/O `.zarr` name fast path returns True without any rc call at all.
    named = "/fake-mounts/s3demo/world.zarr"
    _mount(monkeypatch, {})  # every rc_kind_for would be "missing" if consulted
    allowed, err = server._run_condition(ZARR_CONDITION, named)
    assert allowed is True and err is None


def test_shimmed_gate_fail_closed_on_indeterminate(monkeypatch, guard_kernel):
    # rcd down / rc error / timeout -> every routed probe is "indeterminate",
    # which the shim maps to False (isdir False) so the gate fails closed exactly
    # like a kernel exception. NEVER a fall back to kernel os.* (the guard proves).
    _mount(monkeypatch, {"*": "indeterminate"})
    allowed, err = server._run_condition(ZARR_CONDITION, STORE)
    assert allowed is False and err is None


def test_shimmed_gate_fail_closed_when_read_unavailable(monkeypatch, guard_kernel):
    # zarr.json exists but the bounded serve read fails (no serve / transport
    # error) -> OSError inside the gate -> fail closed, not a group.
    _mount(
        monkeypatch,
        {STORE: "dir", STORE + "/zarr.json": "file"},
        read_bytes=None,  # rc_read_bounded raises OSError
    )
    allowed, err = server._run_condition(ZARR_CONDITION, STORE)
    assert allowed is False and err is None


def test_local_path_gate_uses_kernel_unaffected(tmp_path, guard_kernel):
    # Regression: a NON-mount target keeps the exact current behavior — the gate
    # runs against the real kernel os (the guard only fires for mount paths).
    store = tmp_path / "m"
    store.mkdir()
    (store / ".zgroup").write_text("{}")
    allowed, err = server._run_condition(ZARR_CONDITION, str(store))
    assert allowed is True and err is None
    assert server._run_condition(ZARR_CONDITION, str(tmp_path))[0] is False


# --------------------------------------------------- import-form coverage
#
# The shim routes the gate's `os` off the kernel by overriding __import__ in the
# gate module's builtins. Every idiomatic import form a gate (builtin or
# user-authored) might use must resolve to the shim — `import os`, `import os as
# o`, `from os import path/stat`, and the DOTTED forms `import os.path` /
# `from os.path import isfile`, which call __import__("os.path", ...) and would
# otherwise fall through to the real kernel os.path.


def _gate(tmp_path, body):
    p = tmp_path / "condition.py"
    p.write_text(body)
    return str(p)


def test_import_os_path_dotted_routed(monkeypatch, guard_kernel, tmp_path):
    # `import os.path` -> __import__("os.path") must yield the shim, not real os.
    _mount(monkeypatch, {STORE: "file"})
    cf = _gate(tmp_path, "import os.path\n"
                         "def main(path):\n    return os.path.isfile(path)\n")
    assert server._run_condition(cf, STORE) == (True, None)


def test_from_os_path_import_isfile_routed(monkeypatch, guard_kernel, tmp_path):
    # `from os.path import isfile` -> __import__("os.path", fromlist=("isfile",))
    # must return the shim's path submodule so isfile binds the shimmed function.
    _mount(monkeypatch, {STORE: "file"})
    cf = _gate(tmp_path, "from os.path import isfile\n"
                         "def main(path):\n    return isfile(path)\n")
    assert server._run_condition(cf, STORE) == (True, None)


def test_from_os_path_import_isdir_exists_routed(monkeypatch, guard_kernel, tmp_path):
    _mount(monkeypatch, {STORE: "dir"})
    cf = _gate(tmp_path, "from os.path import isdir, exists\n"
                         "def main(path):\n    return isdir(path) and exists(path)\n")
    assert server._run_condition(cf, STORE) == (True, None)


def test_from_os_import_stat_routed(monkeypatch, guard_kernel, tmp_path):
    import stat as _s
    _mount(monkeypatch, {STORE: "dir"})
    monkeypatch.setattr(
        mounts_mod, "rc_stat_result",
        lambda p: os.stat_result((_s.S_IFDIR | 0o755, 0, 0, 1, 0, 0, 0, 0, 0.0, 0.0)))
    cf = _gate(tmp_path, "from os import stat\n"
                         "def main(path):\n"
                         "    import stat as s\n"
                         "    return s.S_ISDIR(stat(path).st_mode)\n")
    assert server._run_condition(cf, STORE) == (True, None)


def test_import_os_aliased_routed(monkeypatch, guard_kernel, tmp_path):
    _mount(monkeypatch, {STORE: "file"})
    cf = _gate(tmp_path, "import os as o\n"
                         "def main(path):\n    return o.path.isfile(path)\n")
    assert server._run_condition(cf, STORE) == (True, None)


def test_from_os_import_path_aliased_routed(monkeypatch, guard_kernel, tmp_path):
    _mount(monkeypatch, {STORE: "file"})
    cf = _gate(tmp_path, "from os import path as p\n"
                         "def main(path):\n    return p.isfile(path)\n")
    assert server._run_condition(cf, STORE) == (True, None)


# ------------------------------------------------- /api/fs/conditions + /api/fs/stat


def _client():
    return TestClient(server.create_app(start_dir="/"))


def test_conditions_endpoint_true_on_mount_group(monkeypatch, guard_kernel):
    _mount(
        monkeypatch,
        {STORE: "dir", STORE + "/zarr.json": "file"},
        read_bytes=b'{"node_type": "group"}',
    )
    r = _client().get("/api/fs/conditions", params={"path": STORE})
    assert r.status_code == 200
    assert r.json()["conditions"].get("zarr_aoi") is True


def test_conditions_endpoint_200_fail_closed_when_rc_indeterminate(monkeypatch, guard_kernel):
    # rcd unreachable: the endpoint must NOT 404 a path the user just opened —
    # it stays 200 with the gate fail-closed to False.
    _mount(monkeypatch, {"*": "indeterminate"})
    r = _client().get("/api/fs/conditions", params={"path": STORE})
    assert r.status_code == 200
    assert r.json()["conditions"].get("zarr_aoi") is False


def test_conditions_endpoint_404_on_confirmed_missing_mount(monkeypatch, guard_kernel):
    # A healthy rcd confirming the target is gone -> a real 404 (trustworthy
    # negative), never the kernel os.stat.
    _mount(monkeypatch, {"*": "missing"})
    r = _client().get("/api/fs/conditions", params={"path": STORE})
    assert r.status_code == 404


def test_fs_stat_mount_routes_via_rc_not_kernel(monkeypatch, guard_kernel):
    # /api/fs/stat on a mount path synthesizes its stat from rc, never a kernel
    # GETATTR, and reports remote=True.
    _mount(monkeypatch, {STORE: "dir"})

    def _stat_result(p):
        import stat as _s
        return os.stat_result((_s.S_IFDIR | 0o755, 0, 0, 1, 0, 0, 0, 0, 0.0, 0.0))

    monkeypatch.setattr(mounts_mod, "rc_stat_result", _stat_result)
    r = _client().get("/api/fs/stat", params={"path": STORE})
    assert r.status_code == 200
    body = r.json()
    assert body["is_dir"] is True and body["remote"] is True


def test_fs_stat_local_unaffected(tmp_path, guard_kernel):
    # Regression: a local path still stats through the kernel and reports
    # remote=False.
    f = tmp_path / "a.bin"
    f.write_bytes(b"xyz")
    r = _client().get("/api/fs/stat", params={"path": str(f)})
    assert r.status_code == 200
    body = r.json()
    assert body["is_dir"] is False and body["remote"] is False and body["size"] == 3
