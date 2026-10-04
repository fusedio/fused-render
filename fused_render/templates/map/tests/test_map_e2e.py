"""The Map Viewer end to end, in a real browser, against a running server.

Everything the viewer does happens in the page — COGs streamed by range read,
vectors tiled by DuckDB-WASM, Zarr chunks, the document reconciler — so the
page is what is tested: data of every kind is generated locally, loaded
through `window.fusedMap`, and checked for what actually reached the map.

Needs a fused-render server serving THIS checkout's templates, Playwright, and
the geo stack to generate data (this folder's environment):

  FUSED_RENDER_CORE_TEMPLATES=$PWD/fused_render/templates fused-render serve --port 1778
  FUSED_RENDER_URL=http://127.0.0.1:1778 /tmp/mapvenv/bin/python -m pytest \\
      fused_render/templates/map/tests/test_map_e2e.py -o addopts=""

DuckDB-WASM and its spatial extension load from a CDN the first time; the
vector cases skip when it is unreachable.
"""
from __future__ import annotations

import functools
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
np = pytest.importorskip("numpy")
rasterio = pytest.importorskip("rasterio")
gpd = pytest.importorskip("geopandas")
xr = pytest.importorskip("xarray")
pytest.importorskip("h5netcdf")
from rasterio.transform import from_bounds  # noqa: E402
from shapely.geometry import box  # noqa: E402

MAP = Path(__file__).resolve().parents[1]
SERVER = os.environ.get("FUSED_RENDER_URL", "http://127.0.0.1:1778")
TEMPLATE = os.environ.get("MAP_TEMPLATE", str(MAP / "template.html"))
CHROMIUM = os.environ.get("MAP_E2E_CHROMIUM")  # optional explicit executable


def _reachable(url: str) -> bool:
    try:
        urllib.request.urlopen(url, timeout=5).close()
        return True
    except (urllib.error.URLError, OSError):
        return False


@pytest.fixture(scope="module")
def server():
    if not _reachable(f"{SERVER}/api/config"):
        pytest.skip(f"no fused-render server on {SERVER}")
    if not _reachable(f"{SERVER}/template-assets/map.bundle.mjs"):
        pytest.skip("the server does not serve this checkout's map.bundle.mjs")
    return SERVER


@pytest.fixture(scope="module")
def cdn():
    if not _reachable("https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.31.0/package.json"):
        pytest.skip("DuckDB-WASM's CDN is unreachable")


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    d = tmp_path_factory.mktemp("mapdata")
    size = 1024
    yy, xx = np.mgrid[0:size, 0:size]
    dem = (np.sin(xx / 90) * np.cos(yy / 120) * 400 + 1000).astype("float32")
    with rasterio.open(d / "dem.tif", "w", driver="COG", width=size, height=size, count=1,
                       dtype="float32", crs="EPSG:32633", nodata=-9999,
                       transform=from_bounds(500000, 5000000, 510000, 5010000, size, size)) as out:
        out.write(dem, 1)
    rgb = np.stack([xx % 256, yy % 256, (xx + yy) % 256]).astype("uint8")
    with rasterio.open(d / "striped.tif", "w", driver="GTiff", width=size, height=size, count=3,
                       dtype="uint8", crs="EPSG:4326", transform=from_bounds(10, 45, 11, 46, size, size)) as out:
        out.write(rgb)
    rng = np.random.default_rng(1)
    n = 20000
    cx, cy = rng.uniform(-122.5, -122.3, n), rng.uniform(37.7, 37.8, n)
    frame = gpd.GeoDataFrame({"height": rng.uniform(3, 80, n).round(1),
                              "use": rng.choice(["res", "com", "ind"], n)},
                             geometry=[box(x, y, x + 3e-4, y + 2e-4) for x, y in zip(cx, cy)], crs=4326)
    frame.to_parquet(d / "buildings.parquet")
    frame.iloc[:500].to_crs(3857).to_file(d / "small.shp")
    frame.iloc[:500].to_file(d / "small.geojson", driver="GeoJSON")
    t = np.array(["2024-01-01", "2024-02-01", "2024-03-01"], dtype="datetime64[ns]")
    lat, lon = np.linspace(-60, 60, 61), np.linspace(-180, 178, 180)
    cube = (np.cos(np.radians(lat))[None, :, None] * 30 + np.arange(3)[:, None, None] * 5
            + 0 * lon[None, None, :]).astype("float32")
    xr.Dataset({"temp": (("time", "lat", "lon"), cube)},
               coords={"time": t, "lat": lat, "lon": lon}).to_netcdf(d / "temp.nc", engine="h5netcdf")
    return d


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        options = {"args": ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]}
        if CHROMIUM:
            options["executable_path"] = CHROMIUM
        try:
            instance = p.chromium.launch(**options)
        except Exception as error:  # no browser downloaded
            pytest.skip(f"no Chromium for Playwright: {error}")
        yield instance
        instance.close()


class Page:
    def __init__(self, browser, server, **params):
        self.errors = []
        self.page = browser.new_page(viewport={"width": 1280, "height": 800})
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        url = f"{server}/render?" + urllib.parse.urlencode({"path": TEMPLATE, **params})
        self.page.goto(url, wait_until="domcontentloaded")
        self.page.wait_for_function("() => window.fusedMap", timeout=60000)

    def js(self, body, arg=None):
        return self.page.evaluate(f"async (arg) => {{ const fm = window.fusedMap; {body} }}", arg)

    def idle(self, timeout_ms=180000):
        return self.js("return await fm.whenIdle(arg);", timeout_ms)

    def close(self):
        self.page.close()


@pytest.fixture()
def page(browser, server):
    opened = []

    def open_page(**params):
        p = Page(browser, server, **params)
        opened.append(p)
        return p
    yield open_page
    for p in opened:
        assert not p.errors, p.errors
        p.close()


def by_id(facts):
    return {f["id"]: f for f in facts}


def test_rasters_stream_in_the_browser_and_a_striped_tiff_is_converted_once(page, data):
    p = page()
    p.js("fm.execute({op: 'add_layer', source: arg + '/dem.tif', id: 'dem', style: {colormap: 'terrain'}});"
         "fm.execute({op: 'add_layer', source: arg + '/striped.tif', id: 'rgb'});", str(data))
    facts = by_id(p.idle())
    assert facts["dem"]["status"] == "ready" and facts["dem"]["loaded_as"] == "raster"
    assert facts["dem"]["bands"] == 1 and facts["dem"]["rendered"]["colormap"] == "terrain"
    west, south, east, north = facts["dem"]["bounds"]
    assert 14.9 < west < east < 15.2 and 45.1 < south < north < 45.3, "UTM bounds reprojected to degrees"
    assert facts["rgb"]["status"] == "ready" and facts["rgb"]["bands"] == 3
    assert "Cloud-Optimized" in facts["rgb"]["note"], "the striped TIFF went through prepare.py"
    assert facts["rgb"]["rendered"]["mode"] == "rgb"
    # A restyle is a uniform change, not a reload: the same layer, new state.
    p.js("fm.execute({op: 'update_layer', id: 'dem', style: {colormap: 'magma', rescale: [700, 1300]}});")
    time.sleep(0.5)
    assert p.js("return fm.describeLayer('dem').rendered.colormap;") == "magma"


def test_vectors_of_every_format_load_and_big_ones_tile_in_the_browser(page, data, cdn):
    p = page()
    p.js("for (const f of ['buildings.parquet', 'small.shp', 'small.geojson'])"
         "  fm.execute({op: 'add_layer', source: arg + '/' + f});", str(data))
    facts = p.idle()
    assert all(f["status"] == "ready" and f["loaded_as"] == "vector" for f in facts), facts
    big = by_id(facts)["buildings"]
    assert big["features"] == 20000 and set(big["fields"]) >= {"height", "use"}
    shp = by_id(facts)["small"]
    assert shp["features"] == 500
    west, south, east, north = shp["bounds"]
    assert -123 < west < east < -122 and 37 < south < north < 38, "the .prj sidecar was honoured"
    p.js("fm.execute({op: 'update_layer', id: 'buildings', style: {color_by: 'use'}});")
    p.page.wait_for_function("() => (fusedMap.describeLayer('buildings').legend || {}).type === 'cats'", timeout=30000)
    p.js("fm.execute({op: 'update_layer', id: 'buildings', style: {color_by: 'height'}});")
    p.page.wait_for_function("() => (fusedMap.describeLayer('buildings').legend || {}).type === 'ramp'", timeout=30000)


def test_netcdf_is_converted_to_zarr_and_time_steps_through_the_selector(page, data):
    p = page()
    p.js("fm.execute({op: 'add_layer', source: arg + '/temp.nc', id: 't'});", str(data))
    f = by_id(p.idle())["t"]
    assert f["status"] == "ready" and f["loaded_as"] == "zarr", f
    assert f["variables"] == ["temp"]
    time_dim = next(d for d in f["dims"] if d["name"] == "time")
    assert time_dim["size"] == 3 and time_dim["labels"] == ["2024-01-01", "2024-03-01"]
    p.js("fm.execute({op: 'update_layer', id: 't', options: {selector: {time: 2}}, style: {clim: [0, 40]}});")
    assert p.js("return fm.getDocument().layers[0].options.selector.time;") == 2


def test_document_order_is_the_drawing_order_across_engines(page, data):
    p = page()
    p.js("fm.execute({op: 'add_layer', source: arg + '/small.geojson', id: 'v'});"
         "fm.execute({op: 'add_layer', source: arg + '/dem.tif', id: 'r'});"
         "fm.execute({op: 'add_layer', source: arg + '/temp.nc', id: 'z'});", str(data))
    p.idle()

    def drawn():
        order = p.js("return window.__map.getLayersOrder();")
        first = lambda prefix: min(i for i, name in enumerate(order) if name.startswith(prefix))
        return first("v-v-"), first("deck-layer-group"), first("z-z-")
    vector, raster, zarr = drawn()
    assert vector < raster < zarr, "bottom-to-top must follow the document"
    p.js("fm.execute({op: 'move_layer', id: 'v', to: 'top'});")
    time.sleep(0.5)
    vector, raster, zarr = drawn()
    assert raster < zarr < vector


def test_an_open_map_file_follows_agent_edits_and_writes_its_own(page, data, tmp_path):
    if str(MAP) not in sys.path:
        sys.path.insert(0, str(MAP))
    import map_tools

    path = str(tmp_path / "live.fmap")
    assert map_tools.create_map(path, title="Live")["status"] == "ok"
    map_tools.add_layer(path, str(data / "small.geojson"), name="Small")
    p = page(_file=path)
    p.idle()
    assert p.js("return fm.documentPath;") == path
    # An agent edits the file while the page shows it.
    map_tools.add_layer(path, str(data / "dem.tif"), name="DEM", style={"colormap": "viridis"})
    map_tools.set_basemap(path, "dark")
    p.page.wait_for_function("() => fusedMap.getDocument().layers.length === 2"
                             " && fusedMap.getDocument().basemap === 'dark'", timeout=15000)
    assert [f["id"] for f in p.idle()] == ["dem", "small"]
    # The page edits; the file follows.
    p.js("fm.execute({op: 'update_layer', id: 'dem', opacity: 0.4});")
    deadline = time.time() + 10
    while time.time() < deadline:
        on_disk = json.loads(Path(path).read_text())
        if on_disk["layers"][1]["opacity"] == 0.4:
            break
        time.sleep(0.3)
    assert on_disk["layers"][1]["opacity"] == 0.4


def test_refused_commands_throw_and_change_nothing(page):
    p = page()
    before = p.js("return JSON.stringify(fm.getDocument());")
    message = p.js("try { fm.execute({op: 'set_basemap', basemap: 'neon'}); return null; }"
                   " catch (e) { return e.message; }")
    assert "basemap must be one of" in message
    assert p.js("return JSON.stringify(fm.getDocument());") == before


def test_the_postmessage_bridge_answers_a_parent_frame(page):
    p = page()
    reply = p.js("""
      return await new Promise(resolve => {
        window.addEventListener('message', e => { if (e.data && e.data.type === 'fused-map:result') resolve(e.data); });
        window.postMessage({type: 'fused-map:command', requestId: 7, method: 'execute',
                            params: [{op: 'set_title', title: 'From a parent'}]}, location.origin);
      });""")
    assert reply == {"type": "fused-map:result", "requestId": 7, "ok": True, "result": {"title": "From a parent"}}


class _CorsHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture()
def remote(data):
    """`data` over plain HTTP, standing in for a remote bucket."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_CorsHandler, directory=str(data)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_a_remote_layer_downloads_to_a_chosen_folder(page, remote, tmp_path):
    dest = tmp_path / "saved"
    dest.mkdir()
    p = page()
    assert p.page.locator("#btn-save").count() == 0, "the map has no Save button"
    p.js("fm.execute({op: 'add_layer', source: arg + '/small.geojson', id: 'web'});", remote)
    assert by_id(p.idle())["web"]["status"] == "ready"
    # The layer menu offers a download for a remote source...
    p.page.locator(".lc[data-id='web'] .lc-dots").click()
    assert "Download…" in p.page.locator("#lmenu").inner_text()
    # ...and choosing it opens the file browser as a folder picker.
    p.page.locator("#lmenu button", has_text="Download…").click()
    p.page.wait_for_function("() => document.querySelector('#fb-open').textContent === 'Download here'", timeout=30000)
    assert p.page.locator("#fb-title").inner_text() == "Download small.geojson"
    p.page.locator("#fb-cancel").click()
    # The same download through the API (the path an agent takes).
    result = p.js("return await fm.download('web', arg);", str(dest))
    assert result["path"] == str(dest / "small.geojson")
    assert json.loads((dest / "small.geojson").read_text())["type"] == "FeatureCollection"
    local = p.js("fm.execute({op: 'add_layer', source: arg, id: 'local'}); return (await fm.whenIdle()).map(f => f.status);",
                 result["path"])
    assert local == ["ready", "ready"]
