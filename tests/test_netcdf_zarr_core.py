"""Directory-enumeration invariants of the netcdf/zarr grid reader (_zarr_core)
+ grid daemon.

A flat zarr array can hold millions of chunk files, so enumerating an array's
chunk directory (os.walk / os.scandir / os.listdir) must never be the way
metadata is discovered. Reading a file by its EXACT path is a single read.

  * _load_meta's non-consolidated fallback discovers arrays WITHOUT ever
    scandir-ing an array/chunk directory (the live, daemon-reachable path).
  * the grid daemon's /meta does not walk a directory store at all.

Pure-python only (os/json); no numpy/zarr needed, so they run in any repo venv.
"""
import importlib.util
import json
import os
import sys


from _thread_scoped import this_thread_only

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
    # a pile of chunk files — a walk/scandir here is what must be avoided
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
    forbidden directory (an array/chunk dir) is ever enumerated — modelling a
    forbidden listing."""

    def __init__(self, monkeypatch, forbidden=()):
        self.scanned = []
        self.forbidden = {os.path.abspath(p) for p in forbidden}
        self._real = os.scandir

        def fake(path):
            ap = os.path.abspath(path)
            self.scanned.append(ap)
            if ap in self.forbidden:
                raise AssertionError(
                    f"scandir on chunk dir {ap} — enumerating a chunk dir is forbidden")
            return self._real(path)

        # Thread-scoped: `Z.os` is the real `os` module, so this patch is
        # process-wide, and under the fused-engine job another package's
        # background thread polls its own directory with glob (see
        # _thread_scoped.py). Recording ITS scandir made `scanned` a list of
        # paths this code never touched.
        monkeypatch.setattr(Z.os, "scandir", this_thread_only(self._real, fake))


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
# cosmetic chunk count (legacy main() pure path)
# --------------------------------------------------------------------------
def test_chunk_stats_local_still_counts(tmp_path):
    store, arr = _flat_store(tmp_path, consolidated=False, n_chunks=5)
    za = {"shape": [100, 100], "chunks": [10, 10]}
    present, total = Z._chunk_stats(store, "temperature", za)
    assert present == 5
    assert total == 100


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
