"""Exercise point-cloud placement, styling and streaming UI updates."""
from pathlib import Path
import shutil
import subprocess

import pytest


TEMPLATE = Path(__file__).resolve().parents[1] / "fused_render/templates/map/template.html"


def _run_js(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to drive the template's JS")
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_streaming_height_default_waits_for_ground():
    html = TEMPLATE.read_text(encoding="utf-8")
    engine = html.split("const pointcloudEngine = ", 1)[1].split("\nconst ENGINES =", 1)[0]
    harness = "const pointcloudEngine = " + engine + r'''
const assert = require("node:assert/strict");
const pcColormap = name => name || "viridis";
const layer = {style: {}, visible: true, opacity: 1};
let base, manual = false, offset = 0;
const writes = [];
let colormap, colorRange;
const ctl = {
  getState: () => ({zOffsetBase: base}),
  setZOffsetEnabled: value => { manual = true; writes.push(["enabled", value]); },
  setZOffset: value => { manual = true; offset = value; writes.push(["offset", value]); },
  setColorScheme() { colormap = "gray"; colorRange = null; },
  setColormap(value) { colormap = value; }, setPointSize() {},
  setColorRange(value) { colorRange = value; },
  clearElevationRange() {}, getHiddenClassifications: () => [],
  setHiddenClassifications() {},
  getAvailableClassifications: () => [], setOpacity() {},
};
const r = {lidar: ctl, applied: {}, info: {}};
pointcloudEngine.update(layer, r);
pointcloudEngine.update(layer, r);
assert.equal(manual, false, "metadata-only updates must leave auto placement active");
assert.deepEqual(writes, []);
// The streaming loader can now calculate its automatic ground adjustment.
if (!manual) { base = 100; offset = -base; }
assert.equal(offset, -100);
pointcloudEngine.update(layer, r);
assert.deepEqual(writes, [["enabled", true], ["offset", -100]]);
pointcloudEngine.update(layer, r);
assert.equal(writes.length, 2, "unchanged styles should not repeat height writes");
// Explicit zero is a valid manual offset, even before any points arrive.
base = undefined;
r.applied = {};
layer.style.z_offset = 0;
pointcloudEngine.update(layer, r);
assert.equal(offset, 0);
assert.equal(r.applied.z, 0);
layer.style.z_offset = 25;
pointcloudEngine.update(layer, r);
assert.equal(offset, 25);
// Clearing a manual offset after data arrives restores ground placement.
base = 100;
delete layer.style.z_offset;
pointcloudEngine.update(layer, r);
assert.equal(offset, -100);
// A loader scheme transition must not replace the saved colormap/range.
layer.style.colormap = "plasma";
layer.style.clim = [5, 10];
pointcloudEngine.update(layer, r);
layer.style.color_scheme = "intensity";
pointcloudEngine.update(layer, r);
assert.equal(colormap, "plasma");
assert.equal(colorRange.absoluteMin, 5);
assert.equal(colorRange.absoluteMax, 10);
'''
    _run_js(harness)


def test_streaming_refresh_preserves_controls_and_style_edits_take_priority():
    html = TEMPLATE.read_text(encoding="utf-8")
    scheduling = html.split("let uiTimer = null;", 1)[1].split("function statusLine", 1)[0]
    harness = "let uiTimer = null;" + scheduling + r'''
const assert = require("node:assert/strict");
let frame, rebuilds = 0, refreshes = 0;
const selected = "cloud", doc = {title: "Test", basemap: "light"};
const runtime = new Map([[selected, {refreshPointcloudDock: () => refreshes++}]]);
const qs = () => ({classList: {contains: () => false}});
const esc = s => s;
const renderLayerCards = () => {};
const renderStyleDock = () => rebuilds++;
const applyBasemap = () => {};
const requestAnimationFrame = callback => {frame = callback; return 1;};
scheduleUi(false);
scheduleUi(false);
frame();
assert.equal(rebuilds, 0, "streamed batches must leave dropdown DOM intact");
assert.equal(refreshes, 1);
scheduleUi(false);
scheduleUi();
scheduleUi(false);
frame();
assert.equal(rebuilds, 1, "a style edit must take priority over batched progress");
'''
    _run_js(harness)
