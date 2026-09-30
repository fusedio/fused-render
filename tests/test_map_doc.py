"""The map document and its commands (fused_render/templates/map/map_doc.py),
the page's JavaScript twin of them (template.html's map-doc-core block), and
the MCP tools built on them (map_tools.py + mcp.toml).

The document is the Map Viewer's single source of truth: the page, its
`window.fusedMap` API and an agent's MCP tools all change a map by applying
these commands. So the Python and the JavaScript must agree exactly — the
parity test runs the page's REAL block under node over the same cases (a copy
would keep passing after the shipping code drifted).
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAP = ROOT / "fused_render" / "templates" / "map"


def _load(name):
    if str(MAP) not in sys.path:
        sys.path.insert(0, str(MAP))
    spec = importlib.util.spec_from_file_location(name, MAP / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


md = _load("map_doc")
tools = _load("map_tools")


def run(commands, doc=None):
    doc = doc or md.new_doc()
    results = []
    for command in commands:
        doc, result = md.apply(doc, command)
        results.append(result)
    return doc, results


# ---- commands ------------------------------------------------------------------

def test_add_layer_names_and_ids_follow_the_source():
    doc, results = run([
        {"op": "add_layer", "source": "/data/My Roads.geojson"},
        {"op": "add_layer", "source": "/other/my_roads.parquet"},
        {"op": "add_layer", "source": "C:\\data\\DEM.tif", "name": "Elevation"},
    ])
    assert [r["id"] for r in results] == ["my-roads", "my-roads-2", "elevation"]
    assert doc["layers"][0]["name"] == "My Roads.geojson"
    assert doc["layers"][2]["source"] == "C:\\data\\DEM.tif"
    assert doc["layers"][0] == {
        "id": "my-roads", "name": "My Roads.geojson", "source": "/data/My Roads.geojson",
        "kind": "auto", "visible": True, "opacity": 1.0, "style": {}, "options": {}}


def test_apply_never_mutates_its_input_and_refusals_leave_the_doc_alone():
    doc = md.new_doc()
    before = json.dumps(doc)
    md.apply(doc, {"op": "add_layer", "source": "/a.tif"})
    assert json.dumps(doc) == before
    with pytest.raises(md.MapDocError):
        md.apply(doc, {"op": "remove_layer", "id": "nope"})
    assert json.dumps(doc) == before


def test_style_keys_are_checked_by_kind():
    doc, _ = run([{"op": "add_layer", "source": "/a.tif", "kind": "raster", "id": "a"}])
    doc, _ = md.apply(doc, {"op": "update_layer", "id": "a", "style": {"colormap": "magma", "rescale": [0, 10]}})
    assert doc["layers"][0]["style"] == {"colormap": "magma", "rescale": [0, 10]}
    with pytest.raises(md.MapDocError, match="unknown key 'fill_color'"):
        md.apply(doc, {"op": "update_layer", "id": "a", "style": {"fill_color": "#ff0000"}})
    with pytest.raises(md.MapDocError, match="rescale"):
        md.apply(doc, {"op": "update_layer", "id": "a", "style": {"rescale": [10, 0]}})
    # null returns a key to its default
    doc, _ = md.apply(doc, {"op": "update_layer", "id": "a", "style": {"colormap": None}})
    assert doc["layers"][0]["style"] == {"rescale": [0, 10]}


def test_an_auto_layer_accepts_any_kinds_keys_and_a_kind_change_drops_foreign_ones():
    doc, _ = run([{"op": "add_layer", "source": "/x.dat", "id": "x",
                   "style": {"colormap": "viridis", "fill_color": "#112233"}}])
    doc, _ = md.apply(doc, {"op": "update_layer", "id": "x", "kind": "vector"})
    assert doc["layers"][0]["style"] == {"colormap": "viridis", "fill_color": "#112233"}
    doc, _ = md.apply(doc, {"op": "update_layer", "id": "x", "kind": "zarr"})
    assert doc["layers"][0]["style"] == {"colormap": "viridis"}


@pytest.mark.parametrize("command", [
    {"op": "add_layer"},
    {"op": "add_layer", "source": "  "},
    {"op": "add_layer", "source": "/a", "kind": "tiles"},
    {"op": "add_layer", "source": "/a", "id": "Bad Id"},
    {"op": "add_layer", "source": "/a", "opacity": 2},
    {"op": "add_layer", "source": "/a", "style": {"line_color": "red"}},
    {"op": "set_basemap", "basemap": "osm"},
    {"op": "set_view"},
    {"op": "set_view", "center": [200, 0]},
    {"op": "set_view", "bounds": [10, 10, 0, 0]},
    {"op": "move_layer", "id": "nope", "to": "top"},
    {"op": "explode"},
    "not a command",
])
def test_bad_commands_are_refused_with_a_reason(command):
    with pytest.raises(md.MapDocError) as info:
        md.apply(md.new_doc(), command)
    assert str(info.value)


def test_move_layer_by_direction_and_index():
    doc, _ = run([{"op": "add_layer", "source": f"/{n}.tif"} for n in "abcd"])
    order = lambda d: [layer["id"] for layer in d["layers"]]
    doc, _ = md.apply(doc, {"op": "move_layer", "id": "a", "to": "top"})
    assert order(doc) == ["b", "c", "d", "a"]
    doc, _ = md.apply(doc, {"op": "move_layer", "id": "a", "to": "down"})
    assert order(doc) == ["b", "c", "a", "d"]
    doc, _ = md.apply(doc, {"op": "move_layer", "id": "d", "index": 99})
    assert order(doc) == ["b", "c", "a", "d"]
    doc, _ = md.apply(doc, {"op": "move_layer", "id": "d", "index": 0})
    assert order(doc) == ["d", "b", "c", "a"]


def test_view_bounds_and_center_replace_each_other():
    doc, _ = run([{"op": "set_view", "bounds": [0, 0, 10, 10]}])
    assert doc["view"]["bounds"] == [0, 0, 10, 10]
    doc, _ = md.apply(doc, {"op": "set_view", "center": [5, 5], "zoom": 7})
    assert "bounds" not in doc["view"] and doc["view"]["center"] == [5, 5]


def test_apply_all_is_all_or_nothing():
    doc = md.new_doc()
    with pytest.raises(md.MapDocError, match="command 1"):
        md.apply_all(doc, [{"op": "add_layer", "source": "/a"}, {"op": "remove_layer", "id": "zzz"}])
    assert doc["layers"] == []


def test_normalize_rebuilds_a_hand_edited_file_under_the_same_rules():
    raw = {"type": "fused-map", "basemap": "dark",
           "view": {"center": [1, 2], "zoom": 3},
           "layers": [{"source": "/a.tif", "style": {"colormap": "magma"}},
                      {"source": "/a.tif"}]}
    doc = md.normalize(raw)
    assert [layer["id"] for layer in doc["layers"]] == ["a", "a-2"]
    assert doc["basemap"] == "dark" and doc["view"]["zoom"] == 3
    with pytest.raises(md.MapDocError):
        md.normalize({"type": "something-else"})
    with pytest.raises(md.MapDocError, match="newer"):
        md.normalize({"type": "fused-map", "version": 99})
    with pytest.raises(md.MapDocError):
        md.normalize({"type": "fused-map", "layers": [{"source": "/a", "opacity": "x"}]})


def test_save_is_atomic_and_round_trips(tmp_path):
    path = tmp_path / "m.fmap"
    doc, _ = run([{"op": "add_layer", "source": "/a.tif"}])
    md.save(str(path), doc)
    assert md.load(str(path)) == doc
    assert [p.name for p in tmp_path.iterdir()] == ["m.fmap"]


# ---- the JavaScript twin -----------------------------------------------------------

PARITY_CASES = [
    [{"op": "add_layer", "source": "/data/My Roads.geojson"},
     {"op": "add_layer", "source": "/other/my_roads.parquet", "opacity": 0.5},
     {"op": "add_layer", "source": "C:\\data\\DEM.tif", "name": "Elevation", "kind": "raster",
      "style": {"colormap": "terrain", "rescale": [[0, 1], [2, 3]], "nodata": -9999}},
     {"op": "update_layer", "id": "my-roads", "style": {"color_by": "class", "fill_opacity": 0.3}},
     {"op": "update_layer", "id": "my-roads", "style": {"color_by": None}, "visible": False},
     {"op": "move_layer", "id": "elevation", "to": "bottom"},
     {"op": "move_layer", "id": "my-roads", "index": 5},
     {"op": "set_view", "center": [10.5, -3.25], "zoom": 8, "pitch": 30},
     {"op": "set_basemap", "basemap": "sat"},
     {"op": "set_title", "title": "Parity"},
     {"op": "remove_layer", "id": "my-roads-2"}],
    [{"op": "add_layer", "source": "/a.nc", "kind": "zarr", "options": {"variable": "t2m", "selector": {"time": 3}}},
     {"op": "update_layer", "id": "a", "options": {"selector": None}, "style": {"clim": [250, 310]}},
     {"op": "set_view", "bounds": [-10, -5, 10, 5]},
     {"op": "clear_layers"}],
    # refusals must refuse on both sides
    [{"op": "add_layer", "source": "/a", "style": {"line_color": "red"}}],
    [{"op": "add_layer", "source": "/a", "kind": "raster", "style": {"bands": [0]}}],
    [{"op": "add_layer", "source": "/a", "id": "a"}, {"op": "add_layer", "source": "/b", "id": "a"}],
    [{"op": "set_view", "zoom": 30}],
    [{"op": "move_layer", "id": "x"}],
    [{"op": "nope"}],
    [{"op": "add_layer", "source": "/a", "kind": "vector", "style": {"point_mode": "hexbin"}}],
    [{"op": "add_layer", "source": "/a", "kind": "vector"}, {"op": "update_layer", "id": "a", "kind": "raster", "style": {"gamma": 1.5}}],
]
NORMALIZE_CASES = [
    {"type": "fused-map", "basemap": "dark", "view": {"center": [1, 2], "zoom": 3},
     "layers": [{"source": "/a.tif", "style": {"colormap": "magma"}}, {"source": "/a.tif", "visible": False}]},
    {"type": "fused-map", "version": 7},
    {"type": "nope"},
]


def _python_trace(commands):
    doc, trace = md.new_doc(), []
    for command in commands:
        try:
            doc, result = md.apply(doc, command)
            trace.append({"ok": True, "result": result, "doc": doc})
        except md.MapDocError:
            trace.append({"ok": False})
    return trace


def _normalize_trace(raw):
    try:
        return {"ok": True, "doc": md.normalize(raw)}
    except md.MapDocError:
        return {"ok": False}


def test_the_page_applies_commands_exactly_as_python_does(tmp_path):
    node = shutil.which("node")
    if not node:  # pragma: no cover - node is on the CI runners
        pytest.skip("node is needed to run the page's map-doc-core block")
    html = (MAP / "template.html").read_text(encoding="utf-8")
    core = re.search(r'<script id="map-doc-core">(.*?)</script>', html, re.S).group(1)
    harness = tmp_path / "harness.js"
    harness.write_text(core + """
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const traces = input.cases.map(commands => {
  let doc = MapDoc.newDoc();
  return commands.map(command => {
    try { const [next, result] = MapDoc.apply(doc, command); doc = next; return { ok: true, result, doc }; }
    catch (error) { if (!(error instanceof MapDoc.MapDocError)) throw error; return { ok: false }; }
  });
});
const normalized = input.normalize.map(raw => {
  try { return { ok: true, doc: MapDoc.normalize(raw) }; }
  catch (error) { if (!(error instanceof MapDoc.MapDocError)) throw error; return { ok: false }; }
});
process.stdout.write(JSON.stringify({ traces, normalized, style: MapDoc.STYLE_KEYS, options: MapDoc.OPTION_KEYS,
                                      basemaps: MapDoc.BASEMAPS, kinds: MapDoc.KINDS }));
""", encoding="utf-8")
    out = subprocess.run([node, str(harness)], input=json.dumps({"cases": PARITY_CASES, "normalize": NORMALIZE_CASES}),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    js = json.loads(out.stdout)
    assert js["style"] == md.STYLE_KEYS
    assert js["options"] == md.OPTION_KEYS
    assert js["basemaps"] == list(md.BASEMAPS) and js["kinds"] == list(md.KINDS)
    for commands, js_trace in zip(PARITY_CASES, js["traces"]):
        assert js_trace == _python_trace(commands), commands
    for raw, js_doc in zip(NORMALIZE_CASES, js["normalized"]):
        assert js_doc == _normalize_trace(raw), raw


# ---- MCP tools ------------------------------------------------------------------------

def test_tools_build_a_map_file_an_agent_can_read_back(tmp_path):
    path = str(tmp_path / "demo.fmap")
    assert tools.create_map(path, title="Demo")["status"] == "ok"
    assert tools.create_map(path)["status"] == "error"  # never overwrites
    added = tools.add_layer(path, "/data/roads.geojson", style={"line_color": "#ff0000"})
    assert added["status"] == "ok" and added["results"] == [{"id": "roads"}]
    tools.add_layer(path, "/data/dem.tif", kind="raster", style='{"colormap": "terrain"}')
    tools.move_layer(path, "roads", to="top")
    tools.set_view(path, bounds="[-10, -5, 10, 5]")
    tools.set_basemap(path, "dark")
    described = tools.describe_map(path)["map"]
    assert described["basemap"] == "dark"
    assert [layer["id"] for layer in described["layers"]] == ["roads", "dem"]  # top first
    assert described["layers"][1]["style"] == {"colormap": "terrain"}
    assert json.loads((tmp_path / "demo.fmap").read_text())["type"] == "fused-map"


def test_tool_refusals_come_back_as_payloads(tmp_path):
    path = str(tmp_path / "m.fmap")
    assert tools.update_layer(path, "ghost", visible=False)["status"] == "error"
    assert "unknown key" in tools.add_layer(path, "/a.tif", kind="raster", style={"fill_color": "#000000"})["message"]
    assert "not valid JSON" in tools.add_layer(path, "/a.tif", style="{oops")["message"]
    assert tools.set_view(path, center_lon=3)["status"] == "error"
    assert tools.add_layer(str(tmp_path / "m.json"), "/a.tif")["status"] == "error"  # not a .fmap
    assert not (tmp_path / "m.fmap").exists(), "a refused first command must not create the file"
    batch = tools.apply_commands(path, [{"op": "add_layer", "source": "/a"}, {"op": "bogus"}])
    assert batch["status"] == "error" and not (tmp_path / "m.fmap").exists()


def test_mcp_toml_declares_every_tool_with_its_current_signature():
    """`fused app serve` publishes exactly mcp.toml's [[tool]] tables; each must
    name a public map_tools function and carry that function's signature, the
    snapshot the MCP panel's drift check compares against."""
    import ast
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover
        pytest.skip("tomllib")
    declared = tomllib.loads((MAP / "mcp.toml").read_text(encoding="utf-8"))["tool"]
    source = (MAP / "map_tools.py").read_text(encoding="utf-8")
    functions = {node.name: node for node in ast.parse(source).body
                 if isinstance(node, ast.FunctionDef) and not node.name.startswith("_") and node.name != "main"}
    assert {t["entrypoint"] for t in declared} == set(functions)
    for tool in declared:
        node = functions[tool["entrypoint"]]
        assert tool["file"] == "map_tools.py"
        assert tool["name"] == "map_" + tool["entrypoint"]
        assert tool["signature"] == f"{node.name}({ast.unparse(node.args)})", tool["name"]
        assert tool["description"] == " ".join(ast.get_docstring(node).split())
