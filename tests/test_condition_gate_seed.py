"""Gate-seed fast path for /api/fs/conditions (fixes #2, #3, #4).

`_conditions_payload` already does ONE rc is_dir probe of the target; the
zarr_aoi gate then RE-probed the same dir (`os.path.isdir`) plus three serial
`os.path.isfile(join(path, marker))` misses. On the anonymous-S3 ookla mount
that's ~10 sequential round trips (~6.8s) for the common non-zarr directory.

The seed threads what the endpoint already knows (the dir kind) AND, for a
direct-list-capable mount, ONE bounded complete listing of the dir's immediate
children into the gate shim, so the gate answers isdir + all three marker
isfile probes locally with ZERO extra network calls. A truncated / failed
listing transparently falls back to today's per-marker rc probe path.

These mirror tests/test_condition_mount_shim.py: the guard_kernel fixture
proves no kernel os.* ever touches a mount path, and the rc helpers are
monkeypatched (they are tested against a real stub rcd elsewhere).
"""
import os

import pytest
from fastapi.testclient import TestClient

import fused_render.shell.mounts as mounts_mod
from fused_render import server

MOUNT_PREFIX = "/fake-mounts/"
STORE = "/fake-mounts/s3demo/store"
ZARR_CONDITION = os.path.join(server.TEMPLATES_DIR, "zarr_aoi", "condition.py")


@pytest.fixture(autouse=True)
def _clear_conditions_cache():
    # /api/fs/conditions caches success payloads by path for a short TTL. These
    # tests reuse a single STORE path across differing listing/mount states and
    # expect each call to recompute, so drop the cache between tests.
    server._CONDITIONS_CACHE.clear()
    yield
    server._CONDITIONS_CACHE.clear()


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
    path (or "*") to a rc_kind_for verdict; `read_bytes` is what rc_read_bounded
    returns for the zarr.json probe."""
    monkeypatch.setattr(mounts_mod, "is_mount_backed",
                        lambda p: isinstance(p, str) and p.startswith(MOUNT_PREFIX))

    def _kind(p, **k):
        return kind_map.get(p, kind_map.get("*", "missing"))

    monkeypatch.setattr(mounts_mod, "rc_kind_for", _kind)

    def _read(p, *a, **k):
        if read_bytes is None:
            raise OSError("no serve")
        return read_bytes

    monkeypatch.setattr(mounts_mod, "rc_read_bounded", _read)


def _direct_list(monkeypatch, *, capable=True, result=None, raises=None):
    """Monkeypatch the direct (unsigned) pager. `result` is (entries, next_token);
    `raises` is an exception instance the pager raises instead."""
    monkeypatch.setattr(mounts_mod, "direct_list_capable", lambda p: capable)

    def _page(p, *, max_keys, continuation=None, timeout=None):
        if raises is not None:
            raise raises
        return result

    monkeypatch.setattr(mounts_mod, "direct_list_page", _page)


def _client():
    return TestClient(server.create_app(start_dir="/"))


# ----------------------------------------------------------------------- fix #2


def test_seed_skips_target_reprobe(monkeypatch, guard_kernel):
    # A seed carrying {STORE: "dir"} lets the gate answer its own isdir(STORE)
    # with no rc call. rc_kind_for raising for STORE proves it was never
    # reprobed; markers return "missing" so the plain dir is False.
    monkeypatch.setattr(mounts_mod, "is_mount_backed",
                        lambda p: isinstance(p, str) and p.startswith(MOUNT_PREFIX))

    def _kind(p, **k):
        if p == STORE:
            raise AssertionError(f"target reprobed: {p}")
        return "missing"

    monkeypatch.setattr(mounts_mod, "rc_kind_for", _kind)
    monkeypatch.setattr(mounts_mod, "rc_read_bounded",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no serve")))

    allowed, err = server._run_condition(
        ZARR_CONDITION, STORE, seed=server._GateSeed(kinds={STORE: "dir"}))
    assert allowed is False and err is None


# ------------------------------------------------------------------- fix #3 + #4


def test_complete_listing_no_markers_false(monkeypatch, guard_kernel):
    # A complete listing (next_token None) with no marker among the children
    # answers all three isfile probes locally -> False, and rc_kind_for RAISES
    # for any marker path, proving not one marker was probed.
    monkeypatch.setattr(mounts_mod, "is_mount_backed",
                        lambda p: isinstance(p, str) and p.startswith(MOUNT_PREFIX))

    def _kind(p, **k):
        if p == STORE:
            return "dir"
        raise AssertionError(f"marker probed despite complete listing: {p}")

    monkeypatch.setattr(mounts_mod, "rc_kind_for", _kind)
    monkeypatch.setattr(mounts_mod, "rc_read_bounded",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no serve")))
    _direct_list(monkeypatch,
                 result=([{"Name": "part-0.parquet", "IsDir": False}], None))

    r = _client().get("/api/fs/conditions", params={"path": STORE})
    assert r.status_code == 200
    assert r.json()["conditions"].get("zarr_aoi") is False


def test_complete_listing_with_zgroup_true(monkeypatch, guard_kernel):
    # A complete listing containing .zgroup -> True with NO marker probe.
    monkeypatch.setattr(mounts_mod, "is_mount_backed",
                        lambda p: isinstance(p, str) and p.startswith(MOUNT_PREFIX))

    def _kind(p, **k):
        if p == STORE:
            return "dir"
        raise AssertionError(f"marker probed despite complete listing: {p}")

    monkeypatch.setattr(mounts_mod, "rc_kind_for", _kind)
    monkeypatch.setattr(mounts_mod, "rc_read_bounded",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no serve")))
    _direct_list(monkeypatch, result=([{"Name": ".zgroup", "IsDir": False}], None))

    r = _client().get("/api/fs/conditions", params={"path": STORE})
    assert r.status_code == 200
    assert r.json()["conditions"].get("zarr_aoi") is True


def test_truncated_listing_falls_back_to_probes(monkeypatch, guard_kernel):
    # A truncated listing (next_token not None) can't prove a marker absent, so
    # the gate must fall back to per-marker rc probes. .zmetadata -> "file"
    # decides True, and we assert a marker WAS probed.
    probed = []
    monkeypatch.setattr(mounts_mod, "is_mount_backed",
                        lambda p: isinstance(p, str) and p.startswith(MOUNT_PREFIX))

    def _kind(p, **k):
        if p != STORE:
            probed.append(p)
        if p == STORE:
            return "dir"
        if p == STORE + "/.zmetadata":
            return "file"
        return "missing"

    monkeypatch.setattr(mounts_mod, "rc_kind_for", _kind)
    monkeypatch.setattr(mounts_mod, "rc_read_bounded",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no serve")))
    _direct_list(monkeypatch,
                 result=([{"Name": "a", "IsDir": False}], "next-tok"))

    r = _client().get("/api/fs/conditions", params={"path": STORE})
    assert r.status_code == 200
    assert r.json()["conditions"].get("zarr_aoi") is True
    assert probed, "expected a marker probe fallback on a truncated listing"


def test_listing_failure_falls_back_to_probes(monkeypatch, guard_kernel):
    # The pager raising (DirectListError) must fail-open to the per-marker rc
    # probe path; provide marker kinds so the verdict is still correct.
    probed = []
    monkeypatch.setattr(mounts_mod, "is_mount_backed",
                        lambda p: isinstance(p, str) and p.startswith(MOUNT_PREFIX))

    def _kind(p, **k):
        if p != STORE:
            probed.append(p)
        if p == STORE:
            return "dir"
        if p == STORE + "/.zmetadata":
            return "file"
        return "missing"

    monkeypatch.setattr(mounts_mod, "rc_kind_for", _kind)
    monkeypatch.setattr(mounts_mod, "rc_read_bounded",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no serve")))
    _direct_list(monkeypatch, raises=mounts_mod.DirectListError("boom"))

    r = _client().get("/api/fs/conditions", params={"path": STORE})
    assert r.status_code == 200
    assert r.json()["conditions"].get("zarr_aoi") is True
    assert probed, "expected a marker probe fallback on a listing failure"


def test_v3_group_via_listing(monkeypatch, guard_kernel):
    # A complete listing containing zarr.json answers isfile locally; the
    # node_type read still runs via rc_read_bounded and node_type=="group"
    # -> True. rc_kind_for RAISES for any marker, proving no marker probe.
    monkeypatch.setattr(mounts_mod, "is_mount_backed",
                        lambda p: isinstance(p, str) and p.startswith(MOUNT_PREFIX))

    def _kind(p, **k):
        if p == STORE:
            return "dir"
        raise AssertionError(f"marker probed despite complete listing: {p}")

    monkeypatch.setattr(mounts_mod, "rc_kind_for", _kind)
    monkeypatch.setattr(mounts_mod, "rc_read_bounded",
                        lambda *a, **k: b'{"node_type": "group"}')
    _direct_list(monkeypatch, result=([{"Name": "zarr.json", "IsDir": False}], None))

    r = _client().get("/api/fs/conditions", params={"path": STORE})
    assert r.status_code == 200
    assert r.json()["conditions"].get("zarr_aoi") is True
