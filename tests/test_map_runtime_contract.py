"""Packaging and architecture contracts for the built-in Map Viewer.

The viewer draws in the browser (template.html + the vendored map.bundle.mjs)
and keeps Python for conversions only (prepare.py). These tests pin the seams
that decision rests on, so a change that quietly brings a tile server back, or
drops the environment the conversions run in, fails here rather than in front
of a user.
"""
from __future__ import annotations

import importlib.util
import json
import random
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]
MAP = ROOT / "fused_render" / "templates" / "map"
VENDOR = ROOT / "fused_render" / "templates" / "vendor"


def _load(name: str):
    if str(MAP) not in sys.path:
        sys.path.insert(0, str(MAP))
    spec = importlib.util.spec_from_file_location(f"map_contract_{name}", MAP / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _template() -> str:
    return (MAP / "template.html").read_text(encoding="utf-8")


def test_clipboard_quotes_are_removed_before_path_resolution():
    discover = _load("discover")
    path = str(ROOT / "README.md")
    for quoted in (f'"{path}"', f"'{path}'", f"“{path}”"):
        assert discover.clean_path(quoted) == path


def test_remote_urls_are_never_environment_expanded(monkeypatch):
    discover = _load("discover")
    monkeypatch.setenv("HOME", "/should/not/appear")
    remote = "HTTPS://example.test/$HOME/~/Scene.TIF?sig=%24x"
    assert discover.clean_path(f'"{remote}"') == "https://example.test/$HOME/~/Scene.TIF?sig=%24x"


def test_the_environment_is_declared_in_the_folder_and_nothing_tiles():
    """map/pyproject.toml is where the conversion stack lives (the geo stack
    left `[bundled]` in D276), it ships a lock, and none of the tile-server
    packages the browser made redundant come back through it."""
    manifest = MAP / "pyproject.toml"
    assert manifest.exists() and (MAP / "uv.lock").exists()
    declared = tomllib.loads(manifest.read_text(encoding="utf-8"))
    deps = {re.split(r"[<>=\[; ]", d, maxsplit=1)[0].lower() for d in declared["project"]["dependencies"]}
    assert {"rasterio", "xarray", "zarr", "pyogrio", "geopandas"} <= deps
    for gone in ("rio-tiler", "matplotlib", "rioxarray", "mapbox-vector-tile", "netcdf4"):
        assert gone not in deps, f"{gone} is a tile-server dependency; the browser renders now"
    # No managed daemon: nothing in this folder serves tiles.
    assert "fused-render" not in declared.get("tool", {}), "the map no longer runs a daemon"
    assert not (MAP / "daemon.py").exists()

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    bundled = " ".join(project["project"]["optional-dependencies"]["bundled"]).lower()
    for package in ("rasterio", "geopandas", "rio-tiler", "xarray", "mapbox-vector-tile"):
        assert package not in bundled


def test_the_page_renders_from_the_vendored_bundle_not_a_tile_server():
    html = _template()
    assert 'import("/template-assets/map.bundle.mjs")' in html
    assert (VENDOR / "map.bundle.mjs").exists()
    assert (VENDOR / "map.worker.bundle.mjs").exists(), (
        "the GeoTIFF decoder worker the bundle points at is missing")
    bundle = (VENDOR / "map.bundle.mjs").read_text(encoding="utf-8", errors="replace")
    assert 'new URL("./map.worker.bundle.mjs",import.meta.url)' in bundle
    # The point-cloud LAZ decoder ships beside the bundle rather than coming
    # from a CDN, so a LAZ/COPC file opens offline.
    assert (VENDOR / "laz-perf.wasm").exists(), "the LAZ decoder the bundle points at is missing"
    assert 'new URL("./laz-perf.wasm",import.meta.url)' in bundle
    assert "unpkg.com/laz-perf" not in bundle
    # The engine-host proxy and its tile URLs belonged to the old daemon.
    for stale in ("api/engines", "map_render.py", "vtile_url", "tile_url"):
        assert stale not in html, f"template still references the tile server: {stale}"


def test_python_runs_only_for_what_the_browser_cannot_read():
    """quickPlan settles every browser-native format without Python; the
    formats prepare.py claims are exactly the rest."""
    prepare = _load("prepare")
    html = _template()
    vector = set(re.findall(r'"(\.[a-z0-9]+)": "(?:geojson|geoparquet|flatgeobuf|csv|geopackage|shapefile|kmz|kml|gml)"', html))
    assert vector == set(prepare.BROWSER_VECTOR) | {".pq"}
    raster = set(re.search(r"const RASTER_EXT = new Set\(\[(.*?)\]\)", html).group(1).replace('"', "").replace(" ", "").split(","))
    assert raster == set(prepare.BROWSER_RASTER)
    multidim = set(re.search(r"const MULTIDIM_FILE = new Set\(\[(.*?)\]\)", html).group(1).replace('"', "").replace(" ", "").split(","))
    assert set(prepare.MULTIDIM_FILES) == multidim


def test_browsable_formats_all_have_a_loading_path():
    discover = _load("discover")
    prepare = _load("prepare")
    readable = (set(prepare.BROWSER_VECTOR) | set(prepare.BROWSER_RASTER) | set(prepare.GDAL_RASTER)
                | set(prepare.MULTIDIM_FILES) | {".pmtiles", ".py"})
    for suffix in discover.VECTOR + discover.RASTER + discover.PMTILES:
        assert suffix in readable, f"the file browser offers {suffix} but nothing loads it"


def test_a_map_script_importing_what_the_venv_lacks_is_told_where_it_ran(tmp_path):
    prepare = _load("prepare")
    script = tmp_path / "layer.py"
    script.write_text("import definitely_not_installed_pkg\n\ndef main():\n    return None\n")
    result = prepare.main(str(script))
    assert result["status"] == "error"
    assert "definitely_not_installed_pkg" in result["message"]
    assert "fused_render/templates/map/pyproject.toml" in result["message"]


def test_prepare_refusals_are_payloads_not_exceptions(tmp_path):
    prepare = _load("prepare")
    assert prepare.main("")["status"] == "error"
    assert prepare.main(str(tmp_path / "missing.tif"))["status"] == "error"
    assert "unknown action" in prepare.main(str(tmp_path), action="nope")["message"]
    weird = tmp_path / "data.xyz"
    weird.write_text("x")
    assert "does not know how to read .xyz" in prepare.main(str(weird))["message"]


@pytest.mark.parametrize("name", ["map_doc.py", "map_tools.py", "geo_paths.py", "discover.py"])
def test_the_mcp_side_imports_nothing_outside_the_stdlib(name):
    """`fused app serve` runs map_tools on whatever interpreter the MCP host
    resolves, so the tool path must not need the geo stack at import."""
    source = (MAP / name).read_text(encoding="utf-8")
    top = [line for line in source.splitlines() if re.match(r"^(import|from) ", line)]
    allowed = {"__future__", "copy", "json", "math", "os", "re", "sys", "tempfile", "typing",
               "pathlib", "urllib", "stat", "string", "time", "map_doc", "geo_paths",
               "private_dir"}
    for line in top:
        module = re.match(r"^(?:from|import) ([\w.]+)", line).group(1).split(".")[0]
        assert module in allowed, f"{name} imports {module} at module level"


def _run_js(script: str, *args: str, timeout: int = 30) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node is needed to run the template's vector engine")
    result = subprocess.run([node, "-e", script, *args], capture_output=True, text=True,
                            encoding="utf-8", timeout=timeout)
    assert result.returncode == 0, result.stderr
    return result.stdout


FAKE_VECTOR_CONTROL = r'''
const assert = require("node:assert/strict");
const CONTAINERS = { "/data/two.gpkg": ["classes", "review_polygon"], "/data/three.zip": ["a", "b", "c"] };
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
class SelectionCancelled extends Error {}
class FakeVectorControl {
  constructor(options) { this.options = options; this.layers = new Map(); this.listed = []; }
  async addData(source, options) {
    if (this.layers.has(options.id)) throw new Error(`Layer "${options.id}" already exists`);
    const path = new URL(source).searchParams.get("path");
    const names = CONTAINERS[path] || [path];
    let chosen = [options.sourceLayer || names[0]];
    if (!options.sourceLayer && !["geojson", "geoparquet", "csv"].includes(options.format)) {
      await sleep(names.length === 2 ? 40 : 5);
      if (names.length > 1) {
        this.listed.push(path);
        const select = this.options.selectLayers;
        const picked = select === false ? null
          : select ? await select(names, { sourceName: options.name, format: options.format })
          : await new Promise(() => {});
        if (picked && picked.length === 0) throw new SelectionCancelled(`No layers were selected from ${path}.`);
        chosen = picked || names;
      }
    }
    const infos = chosen.map(name => {
      const id = chosen.length > 1 ? `${options.id}-${name}` : options.id;
      const info = { id, name: options.name, sourceLayer: options.sourceLayer || (chosen.length > 1 ? name : undefined),
                     bbox: [0, 0, 1, 1], layerIds: [id + "-fill"] };
      this.layers.set(id, info);
      return info;
    });
    return infos[0];
  }
  getLayer(id) { return this.layers.get(id); }
}
const M = { VectorControl: FakeVectorControl };
const map = { addControl() {} };
const location = { origin: "http://render.test" };
const fused = { stat: async () => ({ size: 1 }) };
const shade = hex => hex;
let seq = 0;
'''


def _vector_engine() -> str:
    html = _template()
    between = lambda start, end: start + html.split(start, 1)[1].split(end, 1)[0]
    return between("const isRemote = ", "\nconst RASTER_EXT") + between("const STREAM_BYTES", "\nconst colorBySignature")


def test_a_multi_layer_file_is_listed_never_picked_and_each_load_is_one_control_layer():
    _run_js(FAKE_VECTOR_CONTROL + _vector_engine() + r'''
const layer = (id, options = {}) => ({ id, name: id, visible: true, opacity: 1, style: {}, options });
const cases = [
  [layer("two"), { load: "vector", path: "/data/two.gpkg", format: "geopackage" }],
  [layer("three"), { load: "vector", path: "/data/three.zip", format: "shapefile" }],
  [layer("one"), { load: "vector", path: "/data/one.gpkg", format: "geopackage" }],
  [layer("pinned", { source_layer: "review_polygon" }), { load: "vector", path: "/data/two.gpkg", format: "geopackage" }],
  [layer("roads"), { load: "vector", path: "/data/roads.geojson", format: "geojson" }],
];
const runs = cases.map(([l, plan]) => { const r = { color: "#336699" }; return vector.add(l, plan, r).then(() => r); });
const hung = sleep(5000).then(() => { throw new Error("a vector load never resolved"); });
Promise.race([Promise.all(runs), hung]).then(([two, three, one, pinned, roads]) => {
  assert.deepEqual(two.sourceLayers, ["classes", "review_polygon"]);
  assert.deepEqual(three.sourceLayers, ["a", "b", "c"]);
  assert.equal(two.info, undefined);
  assert.equal(three.info, undefined);
  for (const r of [one, pinned, roads]) {
    assert.equal(r.sourceLayers, undefined);
    assert.equal(r.info.id, r.handle);
  }
  assert.equal(pinned.info.sourceLayer, "review_polygon");
  assert.deepEqual([...vector.ctl.layers.keys()].sort(), [one.handle, pinned.handle, roads.handle].sort());
  assert.deepEqual(vector.ctl.listed.sort(), ["/data/three.zip", "/data/two.gpkg"]);
  assert.throws(() => vector.ctl.options.selectLayers(["x", "y"], { sourceName: "x", format: "kml" }),
                error => error.sourceLayers.join() === "x,y");
  process.exit(0);
}).catch(error => { console.error(error); process.exit(1); });
''')


def test_the_bundle_stylesheet_carries_the_vector_librarys_own():
    css = (VENDOR / "map.bundle.css").read_text(encoding="utf-8")
    for selector in (".vector-control-panel {", ".vector-control-popup {", ".vector-control-popup-table td {"):
        assert selector in css, selector
    build = (ROOT / "scripts" / "vendor-map" / "build.sh").read_text(encoding="utf-8")
    assert "maplibre-gl-vector/dist/maplibre-gl-vector.css" in build


def test_a_big_geoparquet_is_read_by_row_group_in_a_worker_the_build_makes():
    html = _template()
    assert 'new Worker("/template-assets/parquet.worker.bundle.mjs", { type: "module" })' in html
    assert (VENDOR / "parquet.worker.bundle.mjs").exists()
    build = (ROOT / "scripts" / "vendor-map" / "build.sh").read_text(encoding="utf-8")
    assert "parquet-worker.mjs" in build and "$VENDOR/parquet.worker.bundle.mjs" in build
    package = (ROOT / "scripts" / "vendor-map" / "package.json").read_text(encoding="utf-8")
    assert '"hyparquet":' in package and '"hyparquet-compressors":' in package


def _geoparquet(path: Path, crs=None) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")

    def square(x, y, size=0.01):
        ring = [(x, y), (x + size, y), (x + size, y + size), (x, y + size), (x, y)]
        return struct.pack("<BIII", 1, 3, 1, len(ring)) + b"".join(struct.pack("<dd", *p) for p in ring)

    rng = random.Random(5)
    rows = []
    for qx, qy in ((0, 0), (1, 0), (0, 1), (1, 1)):
        for _ in range(500):
            x, y = 10 + qx + rng.random() * 0.9, 40 + qy + rng.random() * 0.9
            rows.append((x, y))
    table = pa.table({
        "id": pa.array([f"{rng.getrandbits(160):040x}" for _ in rows]),
        "geometry": pa.array([square(x, y) for x, y in rows], pa.binary()),
        "bbox": pa.array([{"xmin": x, "ymin": y, "xmax": x + 0.01, "ymax": y + 0.01} for x, y in rows],
                         pa.struct([(k, pa.float32()) for k in ("xmin", "ymin", "xmax", "ymax")])),
        "class": pa.array([rng.choice(["house", None]) for _ in rows]),
        "height": pa.array([round(rng.random() * 30, 1) for _ in rows]),
    })
    column = {"encoding": "WKB", "geometry_types": ["Polygon"]}
    if crs is not None:
        column["crs"] = crs
    geo = {"version": "1.0.0", "primary_column": "geometry", "columns": {"geometry": column}}
    table = table.replace_schema_metadata({b"geo": json.dumps(geo).encode()})
    pq.write_table(table, path, row_group_size=500)


def _worker(path: Path, ops: list) -> list:
    script = r'''
const fs = require("node:fs");
const { pathToFileURL } = require("node:url");
const [bundle, file, ops] = process.argv.slice(1);
const bytes = fs.readFileSync(file);
const sent = [];
globalThis.self = globalThis;
globalThis.postMessage = message => sent.push(message);
globalThis.fetch = async (url, init = {}) => {
  if (init.method === "HEAD") return new Response(null, { status: 200, headers: { "content-length": String(bytes.length) } });
  const [start, end] = new Headers(init.headers).get("range").replace("bytes=", "").split("-").map(Number);
  return new Response(bytes.subarray(start, end + 1), { status: 206 });
};
import(pathToFileURL(bundle).href).then(async () => {
  const out = [];
  let key;
  for (const [id, op] of JSON.parse(ops).entries()) {
    await self.onmessage({ data: { id, ...op, key } });
    const reply = sent.pop();
    if (!reply.ok) { out.push({ error: reply.error }); continue; }
    if (op.op === "open") key = reply.result.key;
    const result = reply.result && reply.result.buffer
      ? { ...reply.result, buffer: JSON.parse(new TextDecoder().decode(reply.result.buffer)) } : reply.result;
    out.push(result);
  }
  console.log(JSON.stringify(out));
}).catch(error => { console.error(error); process.exit(1); });
'''
    return json.loads(_run_js(script, str(VENDOR / "parquet.worker.bundle.mjs"), str(path), json.dumps(ops), timeout=60))


def test_the_worker_indexes_row_groups_by_their_bbox_and_reads_one_at_a_time(tmp_path):
    path = tmp_path / "buildings.parquet"
    _geoparquet(path)
    opened, group, attributes = _worker(path, [{"op": "open", "url": "http://bucket.test/buildings.parquet",
                                                "budget": 48 << 20},
                                               {"op": "read", "index": 3}, {"op": "row", "row": 1501}])
    assert opened["rows"] == 2000 and len(opened["rowGroups"]) == 4 and opened["geometryName"] == "geometry"
    assert opened["fields"] == ["class", "height"] and opened["extraFields"] == ["id"]
    boxes = [g["bbox"] for g in opened["rowGroups"]]
    assert all(10 <= b[0] < b[2] <= 12.01 and 40 <= b[1] < b[3] <= 42.01 for b in boxes)
    assert boxes[0][2] < 11 and boxes[3][0] > 11 and boxes[3][1] > 41
    assert opened["extent"][0] >= 10 and opened["extent"][3] <= 42.01
    features = group["buffer"]["features"]
    assert group["count"] == 500 == len(features) and group["types"] == ["Polygon"]
    assert features[1]["id"] == 1501 and set(features[1]["properties"]) <= {"class", "height"}
    x, y = features[1]["geometry"]["coordinates"][0][0]
    assert 11 <= x <= 12 and 41 <= y <= 42
    assert list(attributes) == ["id"] and len(attributes["id"]) == 40


def test_the_worker_declines_what_it_cannot_draw_in_degrees(tmp_path):
    path = tmp_path / "mercator.parquet"
    _geoparquet(path, crs={"id": {"authority": "EPSG", "code": 3857}, "type": "ProjectedCRS"})
    [opened] = _worker(path, [{"op": "open", "url": "http://bucket.test/mercator.parquet", "budget": 48 << 20}])
    assert opened == {"problem": "geometry is in a projected CRS"}
