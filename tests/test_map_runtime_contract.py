"""Packaging and architecture contracts for the built-in Map Viewer.

The viewer draws in the browser (template.html + the vendored map.bundle.mjs)
and keeps Python for conversions only (prepare.py). These tests pin the seams
that decision rests on, so a change that quietly brings a tile server back, or
drops the environment the conversions run in, fails here rather than in front
of a user.
"""
from __future__ import annotations

import importlib.util
import re
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
