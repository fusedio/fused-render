"""Mount-safety of the geotiff daemon's /ltile endpoint.

/ltile renders a single pyramid level of a GeoTIFF through rasterio + a
WarpedVRT (used by the overview-pyramid template). Its original code path did
`os.path.getmtime(file)` (cache key) + `rasterio.open(file)` — kernel stat +
kernel open/mmap of the path. Under a read-only rclone NFS mount those calls
stall 18–30s or DROP the mount, wedging the daemon.

The fix keeps templates mount-AGNOSTIC: the UI passes a `src`
(origin + /api/fs/raw?path=) per request; the daemon asks /api/fs/stat whether a
path is `remote` and, if so, opens it through a GDAL `opener=` that range-reads
/api/fs/raw — no kernel I/O touches the mount. Local files keep the fast kernel
path unchanged.

These tests stand up a threaded localhost HTTP server for the two fs endpoints
(mirroring tests/test_pyramid_mount.py) and exercise the module-level pieces:
_stat_payload, _HttpRangeFile, _ltile_remote gating, and the rasterio opener —
including the phantom-.ovr guard (rasterio available in the repo .venv).
"""

import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fused_render.templates.geotiff import tile_server as ts

# --------------------------------------------------------------------------
# a stand-in for /api/fs/stat + /api/fs/raw (one blob)
# --------------------------------------------------------------------------


class _FakeFS:
    """Serves /api/fs/stat (json) and /api/fs/raw (ranged bytes) for one blob.

    `remote` toggles the stat `remote` flag; `exists` False -> stat 404;
    `honor_range` False -> /api/fs/raw ignores Range and returns 200 whole-body
    (the fallback the reader must still slice correctly). It also records the
    set of paths it was asked about, so a test can assert GDAL never probed for
    sidecars (.ovr/.msk/.aux.xml)."""

    def __init__(self, blob=b"", remote=True, exists=True, honor_range=True, is_dir=False):
        self.blob = blob
        self.remote = remote
        self.exists = exists
        self.honor_range = honor_range
        self.is_dir = is_dir
        self.raw_paths = []
        fs = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.startswith("/api/fs/stat"):
                    if not fs.exists:
                        self.send_response(404)
                        self.end_headers()
                        self.wfile.write(b'{"error":"no such file"}')
                        return
                    body = json.dumps(
                        {
                            "remote": fs.remote,
                            "size": None if fs.is_dir else len(fs.blob),
                            "is_dir": fs.is_dir,
                            "name": "x.tif",
                        }
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/api/fs/raw"):
                    fs.raw_paths.append(self.path)
                    rng = self.headers.get("Range")
                    if rng and fs.honor_range and rng.startswith("bytes="):
                        s, _, e = rng[6:].partition("-")
                        s = int(s)
                        e = int(e) if e else len(fs.blob) - 1
                        chunk = fs.blob[s : e + 1]
                        self.send_response(206)
                        self.send_header("Content-Range", f"bytes {s}-{e}/{len(fs.blob)}")
                        self.send_header("Content-Length", str(len(chunk)))
                        self.end_headers()
                        self.wfile.write(chunk)
                    else:
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(fs.blob)))
                        self.end_headers()
                        self.wfile.write(fs.blob)
                    return
                self.send_response(404)
                self.end_headers()

        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self._srv.server_address[1]
        self._t = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._t.start()

    @property
    def src(self):
        # mirror how the template builds src: origin + /api/fs/raw?path=...
        return f"http://127.0.0.1:{self.port}/api/fs/raw?path=%2Fx.tif"

    def close(self):
        self._srv.shutdown()


@pytest.fixture
def fs():
    servers = []

    def make(**kw):
        s = _FakeFS(**kw)
        servers.append(s)
        return s

    yield make
    for s in servers:
        s.close()


# --------------------------------------------------------------------------
# _stat_payload — full payload (remote + size) / 404 / unreachable
# --------------------------------------------------------------------------


def test_stat_payload_reports_remote_and_size(fs):
    s = fs(blob=b"x" * 321, remote=True)
    p = ts._stat_payload(s.src, "/x.tif")
    assert p["remote"] is True
    assert p["size"] == 321


def test_stat_payload_local_not_remote(fs):
    s = fs(blob=b"y" * 9, remote=False)
    assert ts._stat_payload(s.src, "/x.tif")["remote"] is False


def test_stat_payload_unreachable_returns_none():
    src = "http://127.0.0.1:1/api/fs/raw?path=%2Fx.tif"
    assert ts._stat_payload(src, "/x.tif") is None


# --------------------------------------------------------------------------
# _HttpRangeFile — a seekable file object rasterio's opener can read
# --------------------------------------------------------------------------


def test_http_range_file_read_all_matches_blob(fs):
    blob = bytes((i * 37) % 256 for i in range(5000))
    s = fs(blob=blob)
    f = ts._HttpRangeFile(ts._server_url(s.src, "/api/fs/raw", "/x.tif"), len(blob), block=512)
    assert f.seekable()
    assert f.read() == blob


def test_http_range_file_seek_and_partial_reads(fs):
    blob = bytes((i * 7) % 256 for i in range(5000))
    s = fs(blob=blob)
    f = ts._HttpRangeFile(ts._server_url(s.src, "/api/fs/raw", "/x.tif"), len(blob), block=512)
    f.seek(1000)
    assert f.tell() == 1000
    assert f.read(300) == blob[1000:1300]
    f.seek(490)
    assert f.read(60) == blob[490:550]  # spans a block boundary
    f.seek(-10, io.SEEK_END)
    assert f.read() == blob[-10:]


# --------------------------------------------------------------------------
# _ltile_remote — remote gating for the /ltile chain
# --------------------------------------------------------------------------


def test_ltile_remote_builds_descriptor_for_mount_file(fs):
    s = fs(blob=b"z" * 100, remote=True)
    rem = ts._ltile_remote(s.src, "/mnt/x.tif")
    assert rem is not None
    assert rem["size"] == 100
    assert rem["raw_url"].endswith("/api/fs/raw?path=/mnt/x.tif")
    # cache token is derived from raw_url + size (NO kernel getmtime)
    assert rem["key"] == f"{rem['raw_url']}|100"


def test_ltile_remote_none_for_local_file(fs):
    s = fs(blob=b"z" * 100, remote=False)
    assert ts._ltile_remote(s.src, "/mnt/x.tif") is None


def test_ltile_remote_none_without_src(fs):
    # no src -> stay on the local kernel path (unchanged behavior)
    assert ts._ltile_remote(None, "/mnt/x.tif") is None
    assert ts._ltile_remote("", "/mnt/x.tif") is None


def test_ltile_remote_none_when_unreachable():
    # server down -> presumed local, caller falls back to the kernel path
    src = "http://127.0.0.1:1/api/fs/raw?path=%2Fx.tif"
    assert ts._ltile_remote(src, "/mnt/x.tif") is None


def test_lvl_tok_uses_raw_url_size_for_remote_no_getmtime(monkeypatch):
    # for a remote file the cache token must NOT call os.path.getmtime (which
    # would kernel-stat the mount) — it comes straight from the descriptor.
    monkeypatch.setattr(
        os.path, "getmtime", lambda p: (_ for _ in ()).throw(AssertionError("getmtime"))
    )
    rem = {
        "raw_url": "http://h/api/fs/raw?path=/x.tif",
        "size": 5,
        "key": "http://h/api/fs/raw?path=/x.tif|5",
    }
    assert ts._lvl_tok("/mnt/x.tif", rem) == rem["key"]


# --------------------------------------------------------------------------
# _rio_open — the GDAL opener, incl. the phantom-.ovr guard. Needs rasterio
# (present in the repo .venv; skipped otherwise).
# --------------------------------------------------------------------------


def _make_tif(path, with_overviews):
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin

    data = (np.random.default_rng(0).random((3, 1024, 1024)) * 255).astype("uint8")
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=1024,
        width=1024,
        count=3,
        dtype="uint8",
        crs="EPSG:4326",
        transform=from_origin(0, 10, 0.01, 0.01),
        tiled=True,
        blockxsize=256,
        blockysize=256,
    ) as ds:
        ds.write(data)
        if with_overviews:
            ds.build_overviews([2, 4], Resampling.average)


def test_rio_open_local_reads_real_file(tmp_path):
    pytest.importorskip("rasterio")
    p = str(tmp_path / "local.tif")
    _make_tif(p, with_overviews=True)
    with ts._rio_open(p, None) as ds:  # rem None -> plain kernel open
        assert ds.width == 1024
        assert list(ds.overviews(1)) == [2, 4]


def test_rio_open_remote_no_phantom_overviews(fs, tmp_path):
    # The phantom-.ovr guard: a file with NO overviews must report ZERO
    # overviews. If the opener served the main bytes for a `.ovr` sidecar probe,
    # GDAL would read the base image as a bogus EXTERNAL overview and report
    # dozens of phantom levels. Build with NO overviews, upload the bytes, DELETE
    # the local file (so any accidental kernel open fails loudly), open remote.
    pytest.importorskip("rasterio")
    build = str(tmp_path / "build.tif")
    _make_tif(build, with_overviews=False)
    blob = open(build, "rb").read()
    os.remove(build)
    remote_only = str(tmp_path / "gone" / "x.tif")  # never on disk
    assert not os.path.exists(remote_only)
    s = fs(blob=blob, remote=True)
    rem = ts._ltile_remote(s.src, remote_only)
    with ts._rio_open(remote_only, rem) as ds:
        assert ds.width == 1024
        assert list(ds.overviews(1)) == []  # NOT phantom levels
    # bytes came over /api/fs/raw and the guard rejected sidecar probes:
    # every raw request was for our exact path, never x.tif.ovr/.msk/.aux.xml
    assert s.raw_paths, "expected range reads over /api/fs/raw"
    assert all(".ovr" not in p and ".msk" not in p and ".aux" not in p for p in s.raw_paths)


def test_rio_open_remote_uses_internal_overviews(fs, tmp_path):
    # A file WITH internal overviews: the opener detects them (decimated reads
    # ride the internal overviews, not a whole-file re-download).
    pytest.importorskip("rasterio")
    build = str(tmp_path / "build_ov.tif")
    _make_tif(build, with_overviews=True)
    blob = open(build, "rb").read()
    os.remove(build)
    remote_only = str(tmp_path / "gone" / "ov.tif")
    s = fs(blob=blob, remote=True)
    rem = ts._ltile_remote(s.src, remote_only)
    with ts._rio_open(remote_only, rem) as ds:
        assert list(ds.overviews(1)) == [2, 4]
        # a decimated read through overview_level works over the opener
    with ts._rio_open(remote_only, rem, overview_level=0) as ds2:
        assert ds2.width == 512  # first internal overview (1024/2)
