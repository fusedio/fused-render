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
# fire by hand, a 2D context that RECORDS the two calls that reveal a layout,
# and an rAF queue that is not flushed unless a test asks. Positions are
# ASSIGNED by setData, not simulated, so a snapshot straight after setData
# already shows the final layout; `pump()` exists for the glide — the frames a
# CARRIED-OVER node spends easing from where it was to where the new payload
# put it.
#
# Positions are read back through `arc()` and `fillText()` rather than by
# exposing the node array: those are how the browser learns where a node is, so
# a layout that computes correctly and draws somewhere else still fails here.
# `draw()` walks nodes in payload order, so the Nth arc is the Nth node.
HARNESS = r"""
const fs = require("fs");

const winListeners = {};
globalThis.window = globalThis;
globalThis.devicePixelRatio = 1;
globalThis.addEventListener = (type, fn) => {
  (winListeners[type] = winListeners[type] || []).push(fn);
};
globalThis.removeEventListener = () => {};
let rafQueue = [];
globalThis.requestAnimationFrame = (fn) => rafQueue.push(fn);
globalThis.cancelAnimationFrame = () => {};
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "#888888" });
globalThis.MutationObserver = class { observe() {} disconnect() {} };
globalThis.document = { documentElement: { getAttribute: () => null } };

// Run up to `frames` queued rAF callbacks, letting the sim re-queue as it goes.
function pump(frames) {
  for (let i = 0; i < frames; i++) {
    const due = rafQueue;
    rafQueue = [];
    if (!due.length) return;
    for (const fn of due) fn();
  }
}

const drawn = { arcs: [], texts: [] };
const ctx = new Proxy({}, {
  get: (_target, key) => {
    if (key === "arc") return (x, y) => { drawn.arcs.push([x, y]); };
    if (key === "fillText") return (t, x, y) => { drawn.texts.push([t, x, y]); };
    return () => {};
  },
  set: () => true,
});
// One redraw, reporting where every node was drawn (world space — the stub
// applies no transform) and every string drawn, node labels and band names alike.
// Copies, not the live arrays: `pump()` draws too, and a snapshot handed out by
// reference kept growing as the sim redrew behind it.
function snapshot() {
  drawn.arcs = [];
  drawn.texts = [];
  g.draw();
  return { arcs: drawn.arcs.slice(), texts: drawn.texts.slice() };
}
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
const probe = [];   // whatever a test wants to assert about, in order
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

// A vault with a shape: two notes at the top, two a level down, one two levels
// down, linked into a chain that descends through the folders.
const TREE = {
  focus: null,
  nodes: [
    { id: "top.md", kind: "note", label: "top", dir: "", path: "/v/top.md", degree: 1 },
    { id: "peer.md", kind: "note", label: "peer", dir: "", path: "/v/peer.md", degree: 1 },
    { id: "docs/a.md", kind: "note", label: "a", dir: "docs", path: "/v/docs/a.md", degree: 2 },
    { id: "docs/b.md", kind: "note", label: "b", dir: "docs", path: "/v/docs/b.md", degree: 1 },
    { id: "docs/deep/c.md", kind: "note", label: "c", dir: "docs/deep", path: "/v/docs/deep/c.md", degree: 1 },
  ],
  edges: [
    { source: "top.md", target: "docs/a.md", kind: "link" },
    { source: "docs/a.md", target: "docs/deep/c.md", kind: "link" },
  ],
};

BODY

console.log(JSON.stringify({ opened, created, probe }));
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


# ---------------------------------------------------------------- folder bands
#
# The layered layout (D163). A free force layout let position mean nothing, and
# the feedback was that the picture was unreadable; y now encodes the folder, so
# these pin what "layered" actually means on screen. All of them read positions
# out of the draw calls — see the harness.


def test_notes_are_banded_by_folder_shallowest_first():
    got = _run("g.setData(TREE); probe.push(snapshot().arcs);")
    ys = [y for _x, y in got["probe"][0]]
    top, peer, a, b, c = ys
    # Same folder, same row — for both pairs.
    assert top == peer
    assert a == b
    # Distinct folders, distinct rows, ordered root above docs/ above docs/deep/.
    assert top < a < c
    # Evenly spaced, because a band is a fixed height and these are consecutive.
    assert (a - top) == (c - a)


def test_a_single_folder_graph_is_one_centred_row_that_still_says_the_folder():
    # One folder is one row, centred where the free layout put it. Its NAME is
    # still drawn: "these are all in <folder>" is the answer this layout exists
    # to give, and a one-folder neighbourhood is the common case, not an excuse
    # to withhold it.
    got = _run("""
      g.setData({ focus: null, edges: [], nodes: [
        { id: "docs/a.md", kind: "note", label: "a", dir: "docs", path: "/v/docs/a.md", degree: 0 },
        { id: "docs/b.md", kind: "note", label: "b", dir: "docs", path: "/v/docs/b.md", degree: 0 }] });
      probe.push(snapshot());
    """)
    shot = got["probe"][0]
    assert [y for _x, y in shot["arcs"]] == [200, 200]   # canvas is 400x400
    assert [t for t, _x, _y in shot["texts"]] == ["a", "b", "docs/"]


def test_each_band_is_labelled_with_its_folder():
    got = _run("g.setData(TREE); probe.push(snapshot().texts);")
    drawn = [t for t, _x, _y in got["probe"][0]]
    # The band names are drawn after the node labels, in top-to-bottom order.
    assert drawn[-3:] == ["root", "docs/", "docs/deep/"]


def test_a_ghost_and_a_tag_are_banded_below_every_real_folder():
    # Neither has a folder — a ghost does not exist yet and a tag never did — so
    # neither may be drawn in the root's row, which would claim it lives there.
    got = _run("""
      g.setData({
        focus: null,
        nodes: TREE.nodes.concat([
          { id: "ghost:x", kind: "ghost", label: "X", target: "X", dir: null, degree: 1 },
          { id: "tag:t", kind: "tag", label: "#t", dir: null, degree: 1 }]),
        edges: TREE.edges,
      });
      probe.push(snapshot());
    """)
    shot = got["probe"][0]
    ys = [y for _x, y in shot["arcs"]]
    notes, ghost, tag = ys[:5], ys[5], ys[6]
    assert ghost > max(notes)
    assert tag > ghost
    names = [t for t, _x, _y in shot["texts"]]
    assert names[-5:] == ["root", "docs/", "docs/deep/", "unresolved", "tags"]


def test_the_first_layout_is_already_settled():
    # Positions are assigned, not simulated: nothing may drift after the first
    # draw. This is what makes MD-9's 2-second re-sends invisible — there is no
    # cooling period for a re-send to restart.
    got = _run("""
      g.setData(TREE);
      probe.push(snapshot().arcs);
      pump(400);
      probe.push(snapshot().arcs);
    """)
    before, after = got["probe"]
    assert after == before


def test_a_link_across_folders_lands_its_ends_in_a_column():
    # The payoff of banding: `top.md` → `docs/a.md` → `docs/deep/c.md` is a chain
    # descending the tree, and it should read as a column rather than a
    # staircase. The barycenter ordering is what provides it: each band sorts
    # its notes toward where their neighbours sit.
    got = _run("g.setData(TREE); probe.push(snapshot().arcs);")
    xs = [x for x, _y in got["probe"][0]]
    top, _peer, a, _b, c = xs
    assert abs(top - a) < 40
    assert abs(a - c) < 40


def test_two_notes_in_the_same_folder_are_spaced_for_their_labels():
    # Same-folder notes share a row, so the only room for a label is horizontal:
    # each note owns a slot at least SLOT_MIN wide (its measured label width in
    # a real browser; the harness has no measureText).
    got = _run("g.setData(TREE); probe.push(snapshot().arcs);")
    xs = [x for x, _y in got["probe"][0]]
    assert abs(xs[0] - xs[1]) >= 60     # top.md vs peer.md
    assert abs(xs[2] - xs[3]) >= 60     # docs/a.md vs docs/b.md


def test_a_label_that_would_print_over_another_is_dropped_for_the_frame():
    """The slots guarantee settled labels cannot touch; a user drag can still
    park one node on top of another, and the collision must not be DRAWN.
    The lower-priority label is dropped for the frame — a dot at least says
    "hover me", where a half-read label says something false."""
    got = _run("""
      g.setData(TREE);
      const spots = snapshot().arcs;
      // Drag top.md squarely onto peer.md.
      fire("mousedown", spots[0][0], spots[0][1]);
      fire("mousemove", spots[1][0], spots[1][1]);
      fire("mouseup", spots[1][0], spots[1][1]);
      probe.push(snapshot().texts);
    """)
    labels = [t for t, _x, _y in got["probe"][0]]
    assert ("top" in labels) != ("peer" in labels)   # exactly one survives
    # Everything that was not collided still prints.
    assert {"a", "b", "c"} <= set(labels)


def test_a_note_that_changed_folder_moves_to_its_new_band():
    # Every autosave re-sends the graph (MD-9). A note whose folder changed has
    # to land in the new row rather than keep the old one.
    got = _run("""
      g.setData(TREE);
      pump(400);
      probe.push(snapshot().arcs);
      const moved = JSON.parse(JSON.stringify(TREE));
      moved.nodes[0] = { id: "docs/top.md", kind: "note", label: "top",
                         dir: "docs", path: "/v/docs/top.md", degree: 1 };
      moved.edges = [];
      g.setData(moved);
      pump(400);
      probe.push(snapshot().arcs);
    """)
    before, after = got["probe"]
    # It started in the root band, level with its only fellow root note.
    assert before[0][1] == before[1][1]
    assert before[0][1] < before[2][1]
    # It ends below the root band and inside the docs/ one. Not exactly level
    # with docs/a.md — three notes in a band may wrap onto more than one lane —
    # so the claim is band MEMBERSHIP: it is nearer its new folder-mates than the
    # note it used to share a band with, which no lane offset can fake because
    # BAND_GAP is always wider than LANE_GAP.
    assert after[0][1] > after[1][1]
    assert abs(after[0][1] - after[2][1]) < abs(after[0][1] - after[1][1])


def test_a_busy_folder_is_dealt_into_lanes_so_labels_do_not_collide():
    """A band is one horizontal line, the worst case for labels.

    Observed for real: a nine-note neighbourhood all in one folder rendered as a
    single row and the labels ran together — "READMEauthoring",
    "catalogcomments". Labels are drawn above their node and are wider than they
    are tall, so x-adjacent nodes need to differ in y. Lanes are what provide it,
    and the guarantee under test is that NEIGHBOURS never share one.
    """
    got = _run("""
      const many = [];
      for (let i = 0; i < 9; i++) {
        many.push({ id: "specs/n" + i + ".md", kind: "note", label: "n" + i,
                    dir: "specs", path: "/v/specs/n" + i + ".md", degree: 0 });
      }
      g.setData({ focus: null, nodes: many, edges: [] });
      probe.push(snapshot().arcs);
    """)
    arcs = got["probe"][0]
    # It wrapped: more than one lane, but nowhere near one lane per node.
    lanes = set(y for _x, y in arcs)
    assert 1 < len(lanes) < 9
    # Sorted by x, no two consecutive nodes share a height — which is the actual
    # anti-collision property, not merely "some spread exists".
    by_x = sorted(arcs)
    assert all(a[1] != b[1] for a, b in zip(by_x, by_x[1:]))
    # Lanes are evenly pitched, so the block reads as rows rather than scatter.
    ordered = sorted(lanes)
    assert len({round(b - a, 6) for a, b in zip(ordered, ordered[1:])}) == 1


def test_a_big_folder_graph_stays_banded_and_bounded():
    """A folder of hundreds is the scale case: the grid must still hold.

    300 notes over 10 folders, checked for staying finite, staying grouped into
    exactly ten bands, and staying within the width the slot grid implies — a
    layout bug that scattered assignments would fail all three.
    """
    got = _run("""
      const many = [];
      for (let i = 0; i < 300; i++) {
        const d = "f" + (i % 10);
        many.push({ id: d + "/n" + i + ".md", kind: "note", label: "n" + i,
                    dir: d, path: "/v/" + d + "/n" + i + ".md", degree: 1 });
      }
      g.setData({ focus: null, nodes: many, edges: [] });
      probe.push(snapshot().arcs);
    """)
    arcs = got["probe"][0]
    assert len(arcs) == 300
    assert all(abs(x) < 1e6 and abs(y) < 1e6 for x, y in arcs)
    # Ten folders, each wrapped into however many lanes the 400px-wide stub needs
    # for thirty notes. The structural claim is what matters: the heights group
    # into exactly ten bands, because the gap BETWEEN bands is always larger than
    # the gap between lanes inside one.
    heights = sorted(set(y for _x, y in arcs))
    gaps = [round(b - a, 6) for a, b in zip(heights, heights[1:])]
    lane_gap = min(gaps)
    assert sum(1 for g in gaps if g > lane_gap) == 9   # 9 boundaries → 10 bands
    # Every band is many lanes deep, so this really did wrap rather than pile up.
    assert len(heights) > 10
    # And the rows stayed inside their slot grid: 30 unmeasured notes wrap to a
    # handful of SLOT_MIN columns, nowhere near this.
    spread = max(x for x, _y in arcs) - min(x for x, _y in arcs)
    assert spread < 2000, spread


def test_a_dragged_node_keeps_the_height_you_put_it_at():
    # Drag-to-pin already exempts a node from the sim; it exempts it from the
    # band too, so a node lifted out of its row to be read stays lifted — across
    # a re-send, like every other pin.
    got = _run("""
      g.setData(TREE);
      probe.push(snapshot().arcs);
      const from = snapshot().arcs[0];
      fire("mousedown", from[0], from[1]);
      fire("mousemove", from[0], from[1] + 150);
      fire("mouseup", from[0], from[1] + 150);
      g.setData(TREE);
      pump(400);
      probe.push(snapshot().arcs);
    """)
    before, after = got["probe"]
    assert after[0][1] == before[0][1] + 150
    # Its unpinned neighbour in the same folder did not follow it down.
    assert after[1][1] == before[1][1]
