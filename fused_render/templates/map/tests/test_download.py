"""download.py — copying a remote layer's data to a local folder.

Served from a throwaway local HTTP server so every case is real HTTP: a single
file, a shapefile with its sidecars, and Zarr stores (v2 and v3, consolidated)
whose file lists come from their metadata. Checks the copies are complete and
readable, never overwrite what is there, report progress, and clean up.

Needs `requests` (and zarr/xarray for the store round trips), i.e. this
folder's own environment:
  /tmp/mapvenv/bin/python -m pytest fused_render/templates/map/tests/test_download.py -o addopts=""
"""
from __future__ import annotations

import functools
import importlib.util
import json
import os
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("requests")
np = pytest.importorskip("numpy")
xr = pytest.importorskip("xarray")

MAP = Path(__file__).resolve().parents[1]


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture()
def served(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield root, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture()
def download(tmp_path, monkeypatch):
    if str(MAP) not in sys.path:
        sys.path.insert(0, str(MAP))
    spec = importlib.util.spec_from_file_location("download_under_test", MAP / "download.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PROGRESS_DIR", tmp_path / "progress")
    return module


@pytest.fixture()
def dest(tmp_path):
    folder = tmp_path / "downloads"
    folder.mkdir()
    return folder


def test_a_single_file_is_copied_whole_and_never_overwrites(download, served, dest):
    root, base = served
    payload = bytes(range(256)) * 4000
    (root / "scene.tif").write_bytes(payload)
    plan = download.main(f"{base}/scene.tif", str(dest), "plan")
    assert plan["status"] == "ok" and plan["bytes"] == len(payload) and plan["files"] == 1
    assert plan["target"] == str(dest / "scene.tif")

    first = download.main(f"{base}/scene.tif", str(dest), "run")
    assert first["status"] == "ok" and Path(first["path"]).read_bytes() == payload
    progress = json.loads(Path(plan["progress"]).read_text())
    assert progress["finished"] and progress["done_bytes"] == len(payload) == progress["total_bytes"]

    second = download.main(f"{base}/scene.tif", str(dest), "run")
    assert second["path"] == str(dest / "scene-1.tif"), "an existing file is never replaced"
    assert sorted(p.name for p in dest.iterdir()) == ["scene-1.tif", "scene.tif"]


def test_a_shapefile_brings_the_sidecars_the_server_has(download, served, dest):
    root, base = served
    for suffix in (".shp", ".shx", ".dbf", ".prj"):  # no .cpg on the server
        (root / f"roads{suffix}").write_bytes(suffix.encode() * 10)
    (dest / "roads.shp").write_bytes(b"mine")
    result = download.main(f"{base}/roads.shp", str(dest), "run")
    assert result["status"] == "ok" and result["path"] == str(dest / "roads-1.shp")
    assert sorted(Path(p).name for p in result["written"]) == ["roads-1.dbf", "roads-1.prj", "roads-1.shp", "roads-1.shx"]
    assert (dest / "roads.shp").read_bytes() == b"mine"
    assert not list(dest.glob("*.part"))


def _cube():
    return xr.Dataset(
        {"temp": (("time", "lat", "lon"), np.arange(3 * 20 * 40, dtype="float32").reshape(3, 20, 40))},
        coords={"time": np.arange(3), "lat": np.linspace(-45, 45, 20), "lon": np.linspace(-90, 90, 40)})


@pytest.mark.parametrize("zarr_format", [2, 3])
def test_a_zarr_store_is_listed_from_its_metadata_and_copied_readable(download, served, dest, zarr_format):
    pytest.importorskip("zarr")
    root, base = served
    cube = _cube()
    cube.to_zarr(root / "cube.zarr", zarr_format=zarr_format, consolidated=True,
                 encoding={"temp": {"chunks": (1, 10, 20)}})
    plan = download.main(f"{base}/cube.zarr", str(dest), "plan")
    assert plan["status"] == "ok" and plan["directory"], plan
    assert plan["files"] >= 3 * 2 * 2, "every chunk of temp is listed"
    result = download.main(f"{base}/cube.zarr", str(dest), "run")
    assert result["status"] == "ok" and result["path"] == str(dest / "cube.zarr")
    copy = xr.open_dataset(result["path"], engine="zarr")
    assert np.array_equal(copy["temp"].values, cube["temp"].values)
    assert not (dest / "cube.zarr.part").exists()


def test_a_store_path_naming_its_metadata_downloads_the_store(download, served, dest):
    pytest.importorskip("zarr")
    root, base = served
    _cube().to_zarr(root / "cube.zarr", zarr_format=2, consolidated=True)
    plan = download.main(f"{base}/cube.zarr/.zmetadata", str(dest), "plan")
    assert plan["name"] == "cube.zarr" and plan["directory"]


def test_a_store_without_consolidated_metadata_is_refused(download, served, dest):
    root, base = served
    (root / "bare.zarr").mkdir()
    result = download.main(f"{base}/bare.zarr", str(dest), "plan")
    assert result["status"] == "error" and "consolidated metadata" in result["message"]


@pytest.mark.parametrize("dest_dir, needle", [
    ("relative/place", "absolute path"),
    ("/definitely/not/here", "Not a folder"),
])
def test_a_bad_destination_is_refused(download, served, dest_dir, needle):
    _, base = served
    result = download.main(f"{base}/x.tif", dest_dir, "plan")
    assert result["status"] == "error" and needle in result["message"]


def test_a_missing_file_fails_and_leaves_nothing(download, served, dest):
    _, base = served
    result = download.main(f"{base}/gone.tif", str(dest), "run")
    assert result["status"] == "error" and "no file" in result["message"]
    assert list(dest.iterdir()) == []


def test_local_paths_are_not_downloads(download, dest):
    result = download.main("/data/scene.tif", str(dest), "plan")
    assert result["status"] == "error" and "http" in result["message"]


def test_cancel_stops_a_running_download_and_leaves_nothing(download, served, dest):
    root, base = served
    (root / "big.tif").write_bytes(b"x" * (6 << 20))  # several 1 MB blocks
    plan = download.main(f"{base}/big.tif", str(dest), "plan")
    download.PROGRESS_EVERY = 0
    real_write = download._write_progress

    def cancel_once_bytes_flow(path, **state):
        real_write(path, **state)
        if state["done_bytes"] and not state["finished"]:
            assert download.main(action="cancel", progress=str(path))["status"] == "ok"

    download._write_progress = cancel_once_bytes_flow
    result = download.main(f"{base}/big.tif", str(dest), "run", target=plan["target"])
    assert result["status"] == "ok" and result["cancelled"], result
    assert list(dest.iterdir()) == [], "the partial file is removed"
    download._write_progress = real_write
    again = download.main(f"{base}/big.tif", str(dest), "run", target=plan["target"])
    assert again["path"] == plan["target"] and Path(again["path"]).stat().st_size == 6 << 20, \
        "a stale stop marker never cancels the next download"


def test_cancel_only_touches_progress_files(download):
    assert download.main(action="cancel", progress="/etc/passwd")["status"] == "error"


def _dead_pid():
    import subprocess
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _interrupted(download, target, source, done=None):
    """Leave what a killed run leaves: its parts and an unfinished progress
    file owned by a process that no longer exists."""
    download._write_progress(download._progress_path(target), source=source, finished=False,
                             pid=_dead_pid(), done_bytes=done or 0)


def test_an_interrupted_file_download_resumes_from_where_it_stopped(download, served, dest):
    root, base = served
    payload = bytes(range(256)) * 20000  # ~5 MB
    (root / "scene.tif").write_bytes(payload)
    source = f"{base}/scene.tif"
    # A killed run got the first 1.5 MB into the part.
    (dest / "scene.tif.part").write_bytes(payload[:1_500_000])
    _interrupted(download, dest / "scene.tif", source, 1_500_000)
    plan = download.main(source, str(dest), "plan")
    assert plan["target"] == str(dest / "scene.tif") and plan["resume"]
    # The test server ignores Range (200): the part starts over, still correct.
    result = download.main(source, str(dest), "run")
    assert Path(result["path"]).read_bytes() == payload
    assert list(dest.iterdir()) == [dest / "scene.tif"]


def test_a_range_capable_server_continues_the_part(download, dest, tmp_path, monkeypatch):
    """With a server that honours Range, only the missing tail is fetched."""
    payload = bytes(range(256)) * 20000
    asked = []

    class Response:
        def __init__(self, status, body, headers=None):
            self.status_code, self._body, self.headers = status, body, headers or {}
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def raise_for_status(self): pass
        def iter_content(self, n): 
            for i in range(0, len(self._body), n):
                yield self._body[i:i + n]

    class Session:
        def get(self, url, stream=True, timeout=None, headers=None):
            rng = (headers or {}).get("Range")
            asked.append(rng)
            if rng:
                start = int(rng.split("=")[1].rstrip("-"))
                return Response(206, payload[start:])
            return Response(200, payload)

    part = dest / "scene.tif.part"
    part.write_bytes(payload[:1_000_000])
    seen = []
    download._fetch(Session(), "https://x/scene.tif", dest / "scene.tif", seen.append, tmp_path / "stop")
    assert asked == ["bytes=1000000-"]
    assert (dest / "scene.tif").read_bytes() == payload and not part.exists()
    assert sum(seen) == len(payload), "progress counts the bytes already on disk"


def test_an_interrupted_zarr_copy_keeps_its_finished_chunks(download, served, dest, monkeypatch):
    pytest.importorskip("zarr")
    root, base = served
    cube = _cube()
    cube.to_zarr(root / "cube.zarr", zarr_format=2, consolidated=True,
                 encoding={"temp": {"chunks": (1, 10, 20)}})
    source = f"{base}/cube.zarr"
    staging = dest / "cube.zarr.part"
    (staging / "temp").mkdir(parents=True)
    (staging / "temp" / "0.0.0").write_bytes((root / "cube.zarr" / "temp" / "0.0.0").read_bytes())
    _interrupted(download, dest / "cube.zarr", source)
    fetched = []
    real = download._fetch
    monkeypatch.setattr(download, "_fetch", lambda s, url, *a: fetched.append(url) or real(s, url, *a))
    result = download.main(source, str(dest), "run")
    assert result["path"] == str(dest / "cube.zarr")
    assert not any(u.endswith("/temp/0.0.0") for u in fetched), "the finished chunk is not fetched again"
    copy = xr.open_dataset(result["path"], engine="zarr")
    assert np.array_equal(copy["temp"].values, cube["temp"].values)


def test_a_part_another_run_is_writing_is_never_taken_over(download, served, dest):
    root, base = served
    (root / "scene.tif").write_bytes(b"abc")
    source = f"{base}/scene.tif"
    (dest / "scene.tif.part").write_bytes(b"a")
    download._write_progress(download._progress_path(dest / "scene.tif"), source=source,
                             finished=False, pid=os.getpid())  # alive: this test
    plan = download.main(source, str(dest), "plan")
    assert plan["target"] == str(dest / "scene-1.tif") and not plan["resume"]
    # A different source's leftovers are not resumed either.
    _interrupted(download, dest / "scene.tif", f"{base}/other.tif")
    assert download.main(source, str(dest), "plan")["target"] == str(dest / "scene-1.tif")
