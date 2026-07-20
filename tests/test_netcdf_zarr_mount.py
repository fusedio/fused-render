"""Mount-safety of the netcdf/zarr grid reader (_zarr_core) + grid daemon.

The FATAL operation on a mount-backed zarr store is DIRECTORY ENUMERATION
(os.walk / os.scandir / os.listdir): rclone services one readdir by listing the
whole parent S3 prefix, and on a flat array (millions of chunk files, e.g. MUR
SST) that trips the macOS NFS deadman and DROPS THE MOUNT. Reading a file by its
EXACT path is a single round-trip and is safe.

These tests pin the invariants added on branch fix/template-kernel-listing:

  * _load_meta's non-consolidated fallback discovers arrays WITHOUT ever
    scandir-ing an array/chunk directory (the live, daemon-reachable path).
  * _chunk_stats / _store_summary skip their enumerations for remote stores
    (the legacy _zarr_core.main() 'pure' path).
  * the grid daemon's /meta no longer walks a directory store at all.

Pure-python only (os/json + a threaded localhost stat server); no numpy/zarr
needed, so they run in any repo venv.
"""
import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

NETCDF_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "fused_render", "templates", "netcdf")


def _load_module():
    # _zarr_core does `import _grid_common as G` at top level, so its own
    # directory must be importable — same as the grid daemon (which inserts
    # `here` on sys.path before importing it).
    if NETCDF_DIR not in sys.path:
        sys.path.insert(0, NETCDF_DIR)
    spec = importlib.util.spec_from_file_location(
        "_zarr_core_under_test", os.path.join(NETCDF_DIR, "_zarr_core.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


Z = _load_module()


# --------------------------------------------------------------------------
# a threaded stand-in for /api/fs/stat (only endpoint _is_remote needs)
# --------------------------------------------------------------------------
class _FakeStat:
    def __init__(self, remote=True, exists=True):
        self.remote = remote
        self.exists = exists
        fs = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if not self.path.startswith("/api/fs/stat"):
                    self.send_response(404)
                    self.end_headers()
                    return
                if not fs.exists:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'{"error":"nope"}')
                    return
                body = json.dumps(
                    {"remote": fs.remote, "size": 0, "is_dir": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    @property
    def src(self):
        return f"http://127.0.0.1:{self.port}/api/fs/raw?path=%2Fstore"

    def stop(self):
        self._srv.shutdown()


@pytest.fixture
def fake_stat():
    made = []

    def make(remote=True, exists=True):
        s = _FakeStat(remote=remote, exists=exists)
        made.append(s)
        return s

    yield make
    for s in made:
        s.stop()


# --------------------------------------------------------------------------
# store builders
# --------------------------------------------------------------------------
def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


def _flat_store(tmp_path, consolidated=False, n_chunks=2000):
    """A non-trivial zarr v2 store: root group + one array 'temperature' whose
    directory holds many chunk files. Enumerating that array dir is the fatal
    op we must avoid."""
    store = tmp_path / "sst.zarr"
    arr = store / "temperature"
    arr.mkdir(parents=True)
    _write(str(store / ".zattrs"), {"title": "fake"})
    _write(str(store / ".zgroup"), {"zarr_format": 2})
    zarray = {"shape": [100, 100], "chunks": [10, 10], "dtype": "<f4",
              "compressor": None, "fill_value": None, "order": "C",
              "zarr_format": 2}
    _write(str(arr / ".zarray"), zarray)
    _write(str(arr / ".zattrs"), {"_ARRAY_DIMENSIONS": ["lat", "lon"]})
    # a pile of chunk files — a walk/scandir here is what drops the mount
    for i in range(n_chunks):
        (arr / f"{i}.0").write_bytes(b"\x00")
    if consolidated:
        _write(str(store / ".zmetadata"), {"metadata": {
            ".zattrs": {"title": "fake"},
            "temperature/.zarray": zarray,
            "temperature/.zattrs": {"_ARRAY_DIMENSIONS": ["lat", "lon"]},
        }})
    return str(store), str(arr)


class _ScandirTrap:
    """Patches os.scandir to record every directory scanned and to RAISE if a
    forbidden directory (an array/chunk dir) is ever enumerated — modelling the
    mount deadman firing on that listing."""

    def __init__(self, monkeypatch, forbidden=()):
        self.scanned = []
        self.forbidden = {os.path.abspath(p) for p in forbidden}
        self._real = os.scandir

        def fake(path):
            ap = os.path.abspath(path)
            self.scanned.append(ap)
            if ap in self.forbidden:
                raise AssertionError(
                    f"scandir on chunk dir {ap} — would drop the mount")
            return self._real(path)

        monkeypatch.setattr(Z.os, "scandir", fake)


# --------------------------------------------------------------------------
# _is_remote / _stat over HTTP (mount knowledge behind the server API)
# --------------------------------------------------------------------------
def test_is_remote_true(fake_stat):
    s = fake_stat(remote=True)
    assert Z._is_remote(s.src, "/store") is True


def test_is_remote_false(fake_stat):
    s = fake_stat(remote=False)
    assert Z._is_remote(s.src, "/store") is False


def test_is_remote_no_src_presumes_local():
    # no src -> never touch the network, presume local (matches pyramid)
    assert Z._is_remote("", "/store") is False


def test_is_remote_unreachable_presumes_local():
    # nothing listening -> unreachable -> presumed local
    src = "http://127.0.0.1:1/api/fs/raw?path=%2Fstore"
    assert Z._is_remote(src, "/store") is False


# --------------------------------------------------------------------------
# _load_meta: the LIVE, daemon-reachable path. Must never scandir a chunk dir.
# --------------------------------------------------------------------------
def test_load_meta_nonconsolidated_never_scandirs_array_dir(tmp_path, monkeypatch):
    store, arr = _flat_store(tmp_path, consolidated=False)
    trap = _ScandirTrap(monkeypatch, forbidden=[arr])

    arrays, root_attrs = Z._load_meta(store)

    assert "temperature" in arrays
    assert arrays["temperature"]["zarray"]["shape"] == [100, 100]
    assert root_attrs.get("title") == "fake"
    # the array/chunk directory must NOT have been enumerated
    assert os.path.abspath(arr) not in trap.scanned
    # the group root, by contrast, IS listed (few entries, safe)
    assert os.path.abspath(store) in trap.scanned


def test_load_meta_consolidated_does_not_scandir_at_all(tmp_path, monkeypatch):
    store, arr = _flat_store(tmp_path, consolidated=True)
    trap = _ScandirTrap(monkeypatch, forbidden=[arr, store])

    arrays, root_attrs = Z._load_meta(store)

    assert "temperature" in arrays
    # consolidated metadata (.zmetadata) is read by exact path — zero listing
    assert trap.scanned == []


# --------------------------------------------------------------------------
# cosmetic enumerations skipped for remote stores (legacy main() pure path)
# --------------------------------------------------------------------------
def test_chunk_stats_remote_skips_scandir(tmp_path, monkeypatch):
    store, arr = _flat_store(tmp_path, consolidated=False)

    def boom(path):
        raise AssertionError("os.scandir called for a remote chunk_stats")

    monkeypatch.setattr(Z.os, "scandir", boom)
    za = {"shape": [100, 100], "chunks": [10, 10]}
    present, total = Z._chunk_stats(store, "temperature", za, remote=True)
    assert present is None
    assert total == 100          # ceil(100/10) * ceil(100/10)


def test_chunk_stats_local_still_counts(tmp_path):
    store, arr = _flat_store(tmp_path, consolidated=False, n_chunks=5)
    za = {"shape": [100, 100], "chunks": [10, 10]}
    present, total = Z._chunk_stats(store, "temperature", za, remote=False)
    assert present == 5
    assert total == 100


def test_store_summary_remote_skips_walk(tmp_path, monkeypatch):
    store, arr = _flat_store(tmp_path, consolidated=False)

    def boom(*a, **k):
        raise AssertionError("os.walk called for a remote store_summary")

    monkeypatch.setattr(Z.os, "walk", boom)
    summ = Z._store_summary(store, remote=True)
    assert summ["remote"] is True
    assert summ["size"] is None
    assert summ["files"] is None


# --------------------------------------------------------------------------
# regression guards on the source: no live kernel walk survives a refactor
# --------------------------------------------------------------------------
def test_zarr_core_source_has_no_oswalk():
    with open(os.path.join(NETCDF_DIR, "_zarr_core.py")) as f:
        src = f.read()
    # os.walk must survive only in comments (the INVARIANT explanations), never
    # as the metadata-discovery mechanism.
    assert "for dirpath, _, files in os.walk(store)" not in src


def test_grid_daemon_source_has_no_directory_walk():
    with open(os.path.join(NETCDF_DIR, "grid_tile_server.py")) as f:
        src = f.read()
    assert "for dp, _, fs in os.walk(path)" not in src
    assert "dir_sizes" not in src        # cache for the removed walk is gone
