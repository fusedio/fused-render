"""GeoTIFF streaming and the tile daemon's GDAL engine (D1488).

The geotiff page signs an `s3://` file with `fused.s3.sign` and gives the tile
daemon the HTTPS URL; the daemon reads every file with GDAL, a URL through
/vsicurl/, so a streamed file and a local one draw the same tiles.

These run the real daemon in its own venv (rasterio + GDAL), against files
written by that venv and served by the loopback bucket (tests/_range_bucket.py).
Where that venv isn't built (CI), they skip.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest
from _range_bucket import bucket  # noqa: F401 - the fixture

import fused_render

GEOTIFF_DIR = os.path.join(os.path.dirname(fused_render.__file__), "templates", "geotiff")
if GEOTIFF_DIR not in sys.path:
    sys.path.insert(0, GEOTIFF_DIR)

import tile_server  # noqa: E402 - the stdlib half; the daemon runs in its venv

DAEMON_PY = os.path.join(tile_server.DAEMON_VENV, "bin", "python")


def _daemon_venv_ready() -> bool:
    if not os.path.exists(DAEMON_PY):
        return False
    r = subprocess.run([DAEMON_PY, "-c", "import rasterio, numpy, pyproj"],
                       capture_output=True, timeout=60, check=False)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not _daemon_venv_ready(),
                                reason="the geotiff daemon venv (rasterio) is not built here")

# Files written by the daemon's own rasterio: a 2048² uint8 ramp, with
# overviews, as classic TIFF, BigTIFF and with a colour table; and a 4500²
# file with no overviews (bigger than STREAM_MAX_PX, so zoom-in only).
_WRITE = r"""
import sys, numpy as np, rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
d = sys.argv[1]
ramp = (np.add.outer(np.arange(2048), np.arange(2048)) % 251).astype("uint8")
def write(name, data, overviews=True, res=0.0005, **kw):
    prof = dict(driver="GTiff", width=data.shape[1], height=data.shape[0], count=1,
                dtype="uint8", crs="EPSG:4326", transform=from_origin(10.0, 50.0, res, res),
                tiled=True, blockxsize=256, blockysize=256, compress="deflate")
    cmap = kw.pop("cmap", None)
    prof.update(kw)
    with rasterio.open(f"{d}/{name}", "w", **prof) as ds:
        ds.write(data, 1)
        if cmap:
            ds.write_colormap(1, cmap)
        if overviews:
            ds.build_overviews([2, 4, 8], Resampling.nearest)
write("classic.tif", ramp)
write("big.tif", ramp, BIGTIFF="YES")
write("pal.tif", ramp, cmap={i: (i, 255 - i, (7 * i) % 256, 255) for i in range(256)})
write("flat.tif", np.full((4500, 4500), 7, "uint8"), overviews=False, res=0.0001)
# a CRS with no EPSG code (custom Lambert conformal conic, metres)
from rasterio.crs import CRS
lcc = CRS.from_proj4("+proj=lcc +lat_1=41 +lat_2=47 +lat_0=44 +lon_0=11 +x_0=0 +y_0=0 "
                     "+ellps=GRS80 +units=m +no_defs")
assert lcc.to_epsg() is None
with rasterio.open(f"{d}/lcc.tif", "w", driver="GTiff", width=1024, height=1024, count=1,
                   dtype="uint8", crs=lcc, transform=from_origin(-50000, 50000, 100, 100),
                   tiled=True, blockxsize=256, blockysize=256) as ds:
    ds.write(ramp[:1024, :1024], 1)
# overviews in an external .ovr sidecar, not in the file
write("side.tif", ramp, overviews=False)
with rasterio.Env(TIFF_USE_OVR=True), rasterio.open(f"{d}/side.tif", "r+") as ds:
    ds.build_overviews([2, 4], Resampling.nearest)
"""


@pytest.fixture(scope="module")
def tiffs(tmp_path_factory):
    d = tmp_path_factory.mktemp("tiffs")
    subprocess.run([DAEMON_PY, "-c", _WRITE, str(d)], check=True, timeout=120,
                   capture_output=True)
    return {p.stem: str(p) for p in d.glob("*.tif")}


@pytest.fixture(scope="module")
def daemon(tmp_path_factory):
    """The real tile daemon, from this checkout, on its own state file."""
    d = tmp_path_factory.mktemp("daemon")
    state = str(d / "state.json")
    code = ("import sys; sys.argv = ['x', '--serve']; "
            f"sys.path.insert(0, {GEOTIFF_DIR!r}); import tile_server as T; "
            f"T.STATE = {state!r}; T._serve()")
    p = subprocess.Popen([DAEMON_PY, "-c", code], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    try:
        for _ in range(300):
            if os.path.exists(state):
                try:
                    st = json.load(open(state))
                    break
                except ValueError:
                    pass
            time.sleep(0.1)
        else:
            pytest.fail("tile daemon did not start")

        def get(path, **q):
            q["t"] = st["token"]
            url = f"http://127.0.0.1:{st['port']}{path}?" + urllib.parse.urlencode(q)
            try:
                with urllib.request.urlopen(url, timeout=60) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()
        yield get
    finally:
        p.terminate()
        p.wait(10)


def _meta(get, file):
    code, body = get("/meta", file=file)
    assert code == 200, body
    return json.loads(body)


TILE = "/tile/12/2161/1386.png"   # inside the 2048² rasters


def test_a_streamed_tiff_draws_the_same_tiles_as_the_local_file(daemon, tiffs, bucket):  # noqa: F811
    bucket.objects["/t/classic.tif"] = open(tiffs["classic"], "rb").read()
    url = bucket.url("t/classic.tif")
    m = _meta(daemon, url)
    assert m["supported"] and m["engine"] == "GDAL, streamed"
    assert m["levels"] == [[2048, 2048], [1024, 1024], [512, 512], [256, 256]]
    assert m["file_size"] == len(bucket.objects["/t/classic.tif"])
    assert m["files"] == []                          # the URL never goes back to the page
    assert url not in json.dumps(m)
    assert _meta(daemon, tiffs["classic"])["engine"] == "GDAL"
    assert daemon(TILE, file=url) == daemon(TILE, file=tiffs["classic"])
    assert json.loads(daemon("/value", file=url, lon=10.3, lat=49.7)[1]) == \
        json.loads(daemon("/value", file=tiffs["classic"], lon=10.3, lat=49.7)[1])
    hist = json.loads(daemon("/hist", file=url)[1])
    assert hist["channels"][0]["count"] > 0
    ranged = [r for _, r, _ in bucket.log if r and r != "HEAD"]
    assert ranged, "GDAL read the object without Range requests"


def test_a_bigtiff_draws_like_a_classic_tiff(daemon, tiffs):
    assert _meta(daemon, tiffs["big"])["supported"]
    assert daemon(TILE, file=tiffs["big"]) == daemon(TILE, file=tiffs["classic"])


@pytest.mark.parametrize("streamed", [False, True])
def test_a_colour_table_renders_as_the_file_palette(daemon, tiffs, bucket, streamed):  # noqa: F811
    file = tiffs["pal"]
    if streamed:
        bucket.objects["/t/pal.tif"] = open(file, "rb").read()
        file = bucket.url("t/pal.tif")
    m = _meta(daemon, file)
    assert m["has_palette"] is True
    pal = daemon(TILE, file=file, cmap="palette")
    assert pal[0] == 200 and pal != daemon(TILE, file=file, cmap="viridis")
    assert _meta(daemon, tiffs["classic"])["has_palette"] is False


def test_a_streamed_tiff_without_overviews_draws_only_zoomed_in(daemon, tiffs, bucket):  # noqa: F811
    bucket.objects["/t/flat.tif"] = open(tiffs["flat"], "rb").read()
    url = bucket.url("t/flat.tif")
    m = _meta(daemon, url)
    assert m["supported"] and m["stream_error"] is None
    assert m["min_zoom"] and m["min_zoom"] > 10
    # zoomed out: a transparent tile and no histogram, without reading pixels
    before = len(bucket.log)
    code, empty = daemon("/tile/6/33/21.png", file=url)
    assert code == 200
    hist = json.loads(daemon("/hist", file=url)[1])
    assert hist == {"channels": [], "zoom_in": True}
    assert len(bucket.log) == before
    # zoomed in: real pixels
    z = m["min_zoom"]
    n = 2 ** z
    x = int((10.2 + 180) / 360 * n)
    import math
    lat = math.radians(49.8)
    y = int((1 - math.log(math.tan(lat) + 1 / math.cos(lat)) / math.pi) / 2 * n)
    code, tile = daemon(f"/tile/{z}/{x}/{y}.png", file=url)
    assert code == 200 and tile != empty
    # the same file on disk is drawn whole, as before
    assert _meta(daemon, tiffs["flat"])["min_zoom"] is None


def test_an_unreachable_url_fails_meta_not_the_daemon(daemon):
    code, _ = daemon("/meta", file="http://127.0.0.1:9/nothing.tif")
    assert code == 500
    assert daemon("/ping")[0] in (200, 403)


def test_a_crs_without_an_epsg_code_draws(daemon, tiffs):
    m = _meta(daemon, tiffs["lcc"])
    assert m["supported"] and m["crs"].get("wkt") and m["crs_name"]
    lb = m["lonlat_bounds"]
    assert 10 < (lb["west"] + lb["east"]) / 2 < 12      # near lon_0 = 11
    z, (x, y) = 10, _tile_xy(11.0, 44.0, 10)
    code, tile = daemon(f"/tile/{z}/{x}/{y}.png", file=tiffs["lcc"])
    assert code == 200 and tile != _empty(daemon, tiffs["lcc"])


def test_a_local_file_reads_its_ovr_sidecar(daemon, tiffs):
    assert os.path.exists(tiffs["side"] + ".ovr")
    assert len(_meta(daemon, tiffs["side"])["levels"]) == 3


def test_a_local_file_without_overviews_draws_zoomed_out(daemon, tiffs):
    """No zoom-in-only rule for a local file: a zoomed-out tile is read
    decimated (bounded memory) and has pixels."""
    code, tile = daemon("/tile/6/33/21.png", file=tiffs["flat"])
    assert code == 200 and tile != _empty(daemon, tiffs["flat"])
    assert json.loads(daemon("/hist", file=tiffs["flat"])[1])["channels"][0]["count"] > 0


def _tile_xy(lon, lat, z):
    import math
    n = 2 ** z
    r = math.radians(lat)
    return int((lon + 180) / 360 * n), int((1 - math.log(math.tan(r) + 1 / math.cos(r)) / math.pi) / 2 * n)


def _empty(daemon, file):
    """A tile far from the raster: fully transparent."""
    return daemon("/tile/6/0/0.png", file=file)[1]
