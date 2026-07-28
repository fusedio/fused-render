"""The shared link-graph canvas, driven for real (SPEC §32, MD-19, D157).

`graph-canvas.js` is the one place in the markdown work whose bugs are pure
INTERACTION: a click that should not have happened, a layout that restarts under
you. No source-text assertion catches either, so these tests load the shipping
file under node with a stubbed canvas and dispatch the same mouse events the
browser would — the `_js_block` approach test_map_template_escaping.py uses, for
the same reason (a copy of the logic here would keep passing after the shipping
code regressed).

Both surfaces share this file — the note view's panel and the folder graph mode
— so every behaviour pinned here is pinned for both at once.
"""
import json
import os
import shutil
import subprocess

import pytest

import fused_render

CANVAS = os.path.join(os.path.dirname(os.path.abspath(fused_render.__file__)),
                      "templates", "shared", "graph-canvas.js")

# Enough of a browser for the module to run: a canvas whose listeners we can
# fire by hand, a no-op 2D context, and an rAF queue we deliberately never
# flush, so the sim never steps and every position under test is the one
# setData chose. That is the point — the layout, not the animation, is what
# these tests are about.
HARNESS = r"""
const fs = require("fs");

const winListeners = {};
globalThis.window = globalThis;
globalThis.devicePixelRatio = 1;
globalThis.addEventListener = (type, fn) => {
  (winListeners[type] = winListeners[type] || []).push(fn);
};
globalThis.removeEventListener = () => {};
globalThis.requestAnimationFrame = () => 1;   // queued, never run
globalThis.cancelAnimationFrame = () => {};
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "#888888" });
globalThis.MutationObserver = class { observe() {} disconnect() {} };
globalThis.document = { documentElement: { getAttribute: () => null } };

const ctx = new Proxy({}, { get: () => () => {}, set: () => true });
const listeners = {};
const canvas = {
  width: 0, height: 0, style: {},
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  removeEventListener() {},
  getBoundingClientRect: () => ({ left: 0, top: 0, width: 400, height: 400 }),
  getContext: () => ctx,
};

eval(fs.readFileSync(process.env.CANVAS_JS, "utf8"));

const opened = [];
const created = [];
const g = fusedGraph.create({
  canvas,
  onOpenNote: (path) => opened.push(path),
  onCreateGhost: (target) => created.push(target),
});

function fire(type, x, y) {
  const event = { clientX: x, clientY: y, deltaY: 0, preventDefault() {} };
  for (const fn of listeners[type] || []) fn(event);
  for (const fn of winListeners[type] || []) fn(event);
}
// A press-drag-release, then the click the browser dispatches anyway because
// the press and the release share the element.
function dragTo(fromX, fromY, toX, toY) {
  fire("mousedown", fromX, fromY);
  fire("mousemove", toX, toY);
  fire("mouseup", toX, toY);
  fire("click", toX, toY);
}
function clickAt(x, y) {
  fire("mousedown", x, y);
  fire("mouseup", x, y);
  fire("click", x, y);
}

// One node, declared the focus, so setData puts it dead centre of the 400x400
// canvas: (200, 200) is over it and nothing else is nearby.
const ONE = {
  focus: "a",
  nodes: [{ id: "a", kind: "note", label: "A", path: "/vault/a.md", degree: 1 }],
  edges: [],
};

BODY

console.log(JSON.stringify({ opened, created }));
"""


def _run(body):
    node = shutil.which("node")
    if not node:  # pragma: no cover - node is preinstalled on the CI runners
        pytest.skip("node is required to drive the canvas")
    script = HARNESS.replace("BODY", body)
    env = dict(os.environ, CANVAS_JS=CANVAS)
    out = subprocess.run([node, "-e", script], capture_output=True, text=True,
                         timeout=60, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_plain_click_on_a_node_opens_it():
    # The behaviour everything below must not break.
    got = _run("g.setData(ONE); clickAt(200, 200);")
    assert got["opened"] == ["/vault/a.md"]


def test_dragging_a_node_does_not_open_it():
    """Repositioning a node used to navigate to it.

    `onUp` clears `drag`, but the browser still dispatches `click` on the canvas
    — a press and a release on the same element make a click however far the
    pointer travelled in between — and `at(event)` then finds the node sitting
    under the pointer at its NEW position. So every drag ended in opening the
    note you were only trying to move, which in the note view means leaving the
    document you were editing.
    """
    got = _run("g.setData(ONE); dragTo(200, 200, 300, 260);")
    assert got["opened"] == []


def test_a_jittery_click_still_opens_the_note():
    # The guard is a distance threshold, not a "did any mousemove happen" flag:
    # a mouse that shifts a pixel between press and release is a click.
    got = _run("""
      g.setData(ONE);
      fire("mousedown", 200, 200);
      fire("mousemove", 201, 200);
      fire("mouseup", 201, 200);
      fire("click", 201, 200);
    """)
    assert got["opened"] == ["/vault/a.md"]


def test_resending_the_same_graph_keeps_the_layout_where_it_was():
    """Every autosave re-sends the graph (MD-9) — it must not re-explode it.

    `setData` mapped every node back onto the seeding spiral and set `alpha = 1`,
    so typing with the panel open restarted the layout every 2 seconds: the
    exact "yank the view out from under someone mid-read" frameAll's comment
    says it avoids. Here the node is dragged to a known spot and the same
    payload is re-sent; the node has to still be there, which is what makes the
    click land.
    """
    got = _run("""
      g.setData(ONE);
      fire("mousedown", 200, 200);
      fire("mousemove", 300, 260);
      fire("mouseup", 300, 260);
      g.setData(ONE);
      clickAt(300, 260);
    """)
    assert got["opened"] == ["/vault/a.md"]


def test_a_node_that_was_never_seen_before_is_the_only_one_seeded():
    # Carrying positions over must not mean pinning the graph forever: a node
    # the payload has just gained is seeded, so a new ghost still finds a place.
    got = _run("""
      g.setData(ONE);
      fire("mousedown", 200, 200);
      fire("mousemove", 300, 260);
      fire("mouseup", 300, 260);
      g.setData({
        focus: "a",
        nodes: ONE.nodes.concat([
          { id: "g", kind: "ghost", label: "G", target: "G", degree: 1 }]),
        edges: [{ source: "a", target: "g", kind: "wikilink" }],
      });
      clickAt(300, 260);
    """)
    # The carried node is still where it was left, and the new one did not land
    # on top of it.
    assert got["opened"] == ["/vault/a.md"]
    assert got["created"] == []
