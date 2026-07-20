"""Mount-safety of the overview-pyramid template.

The pyramid inspector must never touch a mount-backed file through the kernel
(os.stat / open / mmap / GDAL): those calls risk 18-30s stalls and can drop the
rclone NFS mount. All mount knowledge flows through the server's HTTP endpoints
(/api/fs/stat -> {"remote": bool, "size": int}, /api/fs/raw -> ranged bytes),
reached via the `src` origin the template passes in. These tests exercise the
pure-python pieces without rasterio/tifffile: URL building, the range reader,
the seekable HTTP file object, remote gating in main(), and the build/cogify
refusal. A threaded localhost HTTP server stands in for the two fs endpoints.
"""
import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fused_render.templates.pyramid import overview_pyramid as op


# --------------------------------------------------------------------------
# a stand-in for /api/fs/stat + /api/fs/raw
# --------------------------------------------------------------------------

class _FakeFS:
    """Serves /api/fs/stat (json) and /api/fs/raw (ranged bytes) for one blob.

    `remote` toggles the stat `remote` flag; `exists` False -> stat 404;
    `honor_range` False -> /api/fs/raw ignores Range and returns 200 whole-body
    (the fallback path the reader must handle)."""

    def __init__(self, blob=b"", remote=True, exists=True, honor_range=True):
        self.blob = blob
        self.remote = remote
        self.exists = exists
        self.honor_range = honor_range
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
                    body = json.dumps({
                        "remote": fs.remote, "size": len(fs.blob),
                        "is_dir": False, "name": "x.tif",
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/api/fs/raw"):
                    rng = self.headers.get("Range")
                    if rng and fs.honor_range and rng.startswith("bytes="):
                        s, _, e = rng[6:].partition("-")
                        s = int(s)
                        e = int(e) if e else len(fs.blob) - 1
                        chunk = fs.blob[s:e + 1]
                        self.send_response(206)
                        self.send_header("Content-Range",
                                         f"bytes {s}-{e}/{len(fs.blob)}")
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
# _server_url — origin from src, path rebuilt + quoted
# --------------------------------------------------------------------------

def test_server_url_uses_src_origin_only_and_quotes_path():
    src = "http://host:8123/api/fs/raw?path=%2Fclient%2Fraw%2Fpath.tif"
    url = op._server_url(src, "/api/fs/stat", "/a dir/file.tif")
    assert url.startswith("http://host:8123/api/fs/stat?path=")
    # the daemon's OWN normalized path is used, url-quoted; src's ?path is not.
    assert "client" not in url
    assert url.endswith("/a%20dir/file.tif")


# --------------------------------------------------------------------------
# _stat — ok / missing(404) / unreachable
# --------------------------------------------------------------------------

def test_stat_ok_reports_remote_and_size(fs):
    s = fs(blob=b"x" * 321, remote=True)
    status, payload = op._stat(s.src, "/x.tif")
    assert status == "ok"
    assert payload["remote"] is True
    assert payload["size"] == 321


def test_stat_ok_local_not_remote(fs):
    s = fs(blob=b"y" * 9, remote=False)
    status, payload = op._stat(s.src, "/x.tif")
    assert status == "ok"
    assert payload["remote"] is False


def test_stat_missing_on_404(fs):
    s = fs(exists=False)
    status, payload = op._stat(s.src, "/x.tif")
    assert status == "missing"
    assert payload is None


def test_stat_unreachable_when_no_server():
    # nothing listening on this port -> unreachable, caller falls back to local
    src = "http://127.0.0.1:1/api/fs/raw?path=%2Fx.tif"
    status, payload = op._stat(src, "/x.tif")
    assert status == "unreachable"
    assert payload is None


# --------------------------------------------------------------------------
# _RangeReader — windowed reads + whole-body fallback
# --------------------------------------------------------------------------

def test_range_reader_reads_window(fs):
    blob = bytes(range(256)) * 4
    s = fs(blob=blob)
    r = op._RangeReader(op._server_url(s.src, "/api/fs/raw", "/x.tif"))
    assert r.read(10, 16) == blob[10:26]


def test_range_reader_whole_body_fallback(fs):
    blob = bytes(range(200))
    s = fs(blob=blob, honor_range=False)
    r = op._RangeReader(op._server_url(s.src, "/api/fs/raw", "/x.tif"))
    # server ignored Range and sent the whole body; reader slices its window
    assert r.read(5, 10) == blob[5:15]


# --------------------------------------------------------------------------
# _HttpRangeFile — a seekable file object rasterio/tifffile can read
# --------------------------------------------------------------------------

def test_http_range_file_read_all_matches_blob(fs):
    blob = bytes((i * 37) % 256 for i in range(5000))
    s = fs(blob=blob)
    f = op._HttpRangeFile(op._server_url(s.src, "/api/fs/raw", "/x.tif"),
                          len(blob), block=512)
    assert f.seekable()
    assert f.read() == blob


def test_http_range_file_seek_and_partial_reads(fs):
    blob = bytes((i * 7) % 256 for i in range(5000))
    s = fs(blob=blob)
    f = op._HttpRangeFile(op._server_url(s.src, "/api/fs/raw", "/x.tif"),
                          len(blob), block=512)
    f.seek(1000)
    assert f.tell() == 1000
    assert f.read(300) == blob[1000:1300]
    # seek across a block boundary and read spanning multiple blocks
    f.seek(490)
    assert f.read(60) == blob[490:550]
    # seek from end
    f.seek(-10, io.SEEK_END)
    assert f.read() == blob[-10:]


# --------------------------------------------------------------------------
# main() remote gating
# --------------------------------------------------------------------------

def test_main_remote_missing_returns_not_a_file(fs):
    s = fs(exists=False)
    res = op.main(file="/mnt/x.tif", action="analyze", src=s.src)
    assert "not a file" in res.get("error", "")


def test_main_build_refused_for_remote(fs, monkeypatch):
    s = fs(blob=b"II*\x00" + b"0" * 100, remote=True)
    # if gating fails, these would run; make them explode so the test is honest
    monkeypatch.setattr(op, "_venv_python",
                        lambda: (_ for _ in ()).throw(AssertionError("spawned")))
    res = op.main(file="/mnt/x.tif", action="build", src=s.src)
    assert "remote mount" in res.get("error", "")
    assert not res.get("started")


def test_main_cogify_refused_for_remote(fs, monkeypatch):
    s = fs(blob=b"II*\x00" + b"0" * 100, remote=True)
    monkeypatch.setattr(op, "_venv_python",
                        lambda: (_ for _ in ()).throw(AssertionError("spawned")))
    res = op.main(file="/mnt/x.tif", action="cogify", src=s.src)
    assert "remote mount" in res.get("error", "")
    assert not res.get("started")


def test_main_unreachable_falls_back_to_local(fs, monkeypatch):
    # server unreachable -> presumed local -> today's os.path.isfile probe runs.
    # a nonexistent local path yields the same "not a file" as before.
    src = "http://127.0.0.1:1/api/fs/raw?path=%2Fnope.tif"
    res = op.main(file="/definitely/not/here.tif", action="analyze", src=src)
    assert "not a file" in res.get("error", "")


def test_main_local_no_src_preserves_existing_behavior():
    res = op.main(file="/definitely/not/here.tif", action="analyze")
    assert "not a file" in res.get("error", "")


# --------------------------------------------------------------------------
# remote analyze wiring — worker gets raw_url/remote/size, no kernel probe
# --------------------------------------------------------------------------

def test_main_remote_analyze_passes_raw_url_to_worker(fs, monkeypatch):
    s = fs(blob=b"II*\x00" + b"0" * 100, remote=True)

    captured = {}

    class _Proc:
        returncode = 0
        stdout = json.dumps({"ok": True, "levels": []})
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        return _Proc()

    import subprocess
    monkeypatch.setattr(op, "_venv_python", lambda: "/fake/python")
    monkeypatch.setattr(subprocess, "run", fake_run)

    res = op.main(file="/mnt/x.tif", action="analyze", src=s.src)
    assert "error" not in res or res.get("ok")
    # worker invoked as [python, worker.py, path, action, opts_json]
    opts = json.loads(captured["argv"][4])
    assert opts.get("remote") is True
    assert opts.get("size") == len(s.blob)
    assert opts["raw_url"].endswith("/api/fs/raw?path=/mnt/x.tif")
    assert "/api/fs/raw" in opts["raw_url"]


def test_worker_source_bundles_shared_helpers():
    src = op._worker_source()
    # the shared pure-python helpers are injected so the worker (separate uv
    # venv, no fused_render on path) can build URLs + range-read over HTTP.
    assert "class _RangeReader" in src
    assert "class _HttpRangeFile" in src
    assert "import rasterio" in src
    assert "import tifffile" in src


def test_worker_opener_rejects_sidecar_probes():
    # regression guard: the opener must serve RAW_URL ONLY for the exact
    # identifier. If it returns the main bytes for a `.ovr`/`.msk` probe, GDAL
    # reads the base image as a phantom external overview (a file with NO
    # overviews then reports dozens of bogus levels), defeating the whole
    # no-overview thumbnail bound.
    src = op._worker_source()
    assert 'raise FileNotFoundError(p)' in src
    assert 'REMOTE_ID' in src


# --------------------------------------------------------------------------
# end-to-end: run the REAL worker source over an HTTP-served GeoTIFF. Skipped
# unless the worker's deps are importable (rasterio in [bundled]; tifffile +
# rio-cogeo are the worker's own uv-venv deps, absent from a plain repo venv).
# --------------------------------------------------------------------------

def _make_tif(path, with_overviews):
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin
    data = (np.random.default_rng(0).random((3, 1024, 1024)) * 255).astype("uint8")
    with rasterio.open(path, "w", driver="GTiff", height=1024, width=1024,
                       count=3, dtype="uint8", crs="EPSG:4326",
                       transform=from_origin(0, 10, 0.01, 0.01),
                       tiled=True, blockxsize=256, blockysize=256) as ds:
        ds.write(data)
        if with_overviews:
            ds.build_overviews([2, 4], Resampling.average)


def _run_worker(fs_srv, path, action="analyze"):
    import subprocess
    import sys
    import tempfile
    raw_url = op._server_url(fs_srv.src, "/api/fs/raw", "/x.tif")
    opts = {"remote": True, "raw_url": raw_url, "size": len(fs_srv.blob)}
    wf = tempfile.mktemp(suffix=".py")
    with open(wf, "w") as f:
        f.write(op._worker_source())
    proc = subprocess.run([sys.executable, wf, path, action, json.dumps(opts)],
                          capture_output=True, text=True, timeout=300)
    os.remove(wf)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


@pytest.mark.parametrize("with_ov", [True, False])
def test_remote_worker_end_to_end(fs, tmp_path, with_ov):
    pytest.importorskip("rasterio")
    pytest.importorskip("tifffile")
    pytest.importorskip("rio_cogeo")
    pytest.importorskip("PIL")
    # Build the TIFF, upload its bytes into the fake HTTP server, then DELETE the
    # local file. The worker is invoked with a path string that no longer exists
    # on disk, so ANY accidental kernel open of `path` (a silent-fallback
    # regression the whole PR exists to prevent) fails loudly instead of quietly
    # returning byte-identical results. All bytes must come over /api/fs/raw.
    build_path = str(tmp_path / "build.tif")
    _make_tif(build_path, with_ov)
    blob = open(build_path, "rb").read()
    os.remove(build_path)
    remote_only_path = str(tmp_path / "gone" / "x.tif")  # never created on disk
    assert not os.path.exists(remote_only_path)
    s = fs(blob=blob, remote=True)
    r = _run_worker(s, remote_only_path)

    # size comes from /api/fs/stat, not a kernel getsize
    assert r["file_size"] == len(s.blob)
    # cog validation is skipped over the mount (opener-incompatible)
    assert r["cog"]["valid"] is None and "skipped" in r["cog"]
    lv0 = r["levels"][0]
    if with_ov:
        assert r["n_overviews"] == 2  # opener detects INTERNAL overviews
        assert lv0["thumb"] is not None  # decimated read served via overviews
        assert lv0["thumb_skipped"] is None
    else:
        # the sidecar-probe fix keeps this at 0 (was 32 phantom levels)
        assert r["n_overviews"] == 0
        assert lv0["thumb"] is None  # full-res decode skipped over the network
        assert lv0["thumb_skipped"]
        assert lv0["crop"] is not None  # a bounded center crop is still served
