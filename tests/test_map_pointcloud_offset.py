"""Exercise the page's height updates before and after streamed points arrive."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_streaming_height_default_waits_for_ground():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required to drive the template's JS")
    html = (Path(__file__).resolve().parents[1] / "fused_render/templates/map/template.html").read_text()
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
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
