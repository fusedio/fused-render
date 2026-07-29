/* Link-graph canvas — shared by the markdown template's local panel and the
 * folder-level `graph` mode (SPEC §32, MD-19). Extracted at the point the
 * second surface appeared, so there is one sim and one set of interaction
 * rules rather than two that drift; served from the /template-shared/ mount
 * (see server.py) like ro-badge.js and sciviz.mjs.
 *
 * Load:  <script src="/template-shared/graph-canvas.js"></script>
 * Use:   const g = fusedGraph.create({ canvas, onOpenNote, onCreateGhost });
 *        g.setData(payload);   // graph.py's `graph` action, verbatim
 *        g.destroy();
 *
 * A hand-rolled O(n²) spring layout on a Canvas 2D surface: no force library
 * is vendored, and at the node counts this view reaches (a bounded
 * neighbourhood, or a folder's notes) the naive sim is well inside budget —
 * vendoring one would be a dependency bought for nothing.
 *
 * The layout is LAYERED BY FOLDER, which is the one place this deliberately
 * stops copying Obsidian (D163). A free force layout answers "what links to
 * what" and nothing else, and the feedback on it was that the picture was
 * unreadable: with every node free in both axes, position carried no meaning, so
 * a web of a dozen notes told you less than the backlinks list did. Here the
 * vertical axis is spent on something: one horizontal band per folder, ordered
 * shallowest-first, so a node's height IS its place in the tree and a chain of
 * links reads as a descent through it. Only x is simulated.
 *
 * Behaviours copied from Obsidian's graph: the layout is fitted to the canvas
 * once it settles, node radius scales with degree,
 * labels fade out past a zoom threshold, hovering lights the neighbourhood,
 * dragging pins a node, ghost nodes are dim with dashed edges, clicking a node
 * opens it. Colours are read from CSS custom properties AT DRAW TIME, because
 * var() cannot resolve inside a canvas fillStyle — so a theme flip redraws
 * rather than repainting nothing (SPEC §30's rule for JS-held colours).
 */
(function () {
  "use strict";

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function create(options) {
    var canvas = options.canvas;
    var onOpenNote = options.onOpenNote || function () {};
    var onCreateGhost = options.onCreateGhost || function () {};

    var nodes = [];
    var edges = [];
    var hover = null;
    var drag = null;
    var pan = null;
    var zoom = 1;
    var ox = 0;
    var oy = 0;
    var alpha = 0;
    var frame = null;
    var dead = false;
    var userFramed = false; // the user panned, zoomed or dragged

    /* A press that TRAVELLED is a drag, not a click. The browser dispatches
     * `click` whenever the press and the release share an element, however far
     * the pointer moved in between, and `at()` then finds the node sitting under
     * the pointer at its new position — so without this, repositioning a node
     * opened it, which in the note view means leaving the document being edited.
     * A threshold rather than a "did any mousemove arrive" flag, because a mouse
     * that shifts a pixel between press and release is still a click. */
    var DRAG_SLOP = 3;  // px of pointer travel
    var pressX = 0;
    var pressY = 0;
    var moved = false;

    function radius(node) { return 3.5 + Math.sqrt(node.degree) * 2.6; }

    /* ---- how far apart nodes settle ------------------------------------------
     * The graph was too dense to read. Linked notes settled ~80px apart (spring
     * rest 70, repulsion 1400) and an 11px label is 60-100px wide, so labels
     * collided at the default zoom. The spacing that matters here is LABEL
     * width, not node radius: a label is drawn ABOVE its node, not inside it.
     * Roughly doubled, which puts linked notes ~145px apart.
     *
     * One sim serves both surfaces (the note view's panel and the folder mode,
     * D157) and their node counts differ by orders of magnitude — but the
     * spacing does NOT need to be scaled per surface, because frameAll() fits
     * the layout to the canvas: scaling the whole layout uniformly changes
     * nothing about what reaches the screen. What varies per surface is the
     * ZOOM, which is exactly the knob that should vary.
     */
    var REST = 135;
    var REPULSION = 4600;
    var MAX_SPEED = 22;   // px per step, before alpha

    /* ---- folder bands --------------------------------------------------------
     * y belongs to the LAYOUT, not to the sim. Every node sits at the centreline
     * of its folder's band, and only x is simulated — which is what makes height
     * mean something (see the file header).
     *
     * One band per DISTINCT folder, not per depth number: two sibling folders
     * are different places and get their own rows, which is the question being
     * asked ("which folder is the note that links here in?"). Ordered by depth
     * then name, so the ordering is stable across re-sends and reads top-down as
     * the tree does.
     *
     * A node with no folder gets a trailing band: a ghost has no folder because
     * it does not exist yet, and a tag never had one. They are kept OUT of the
     * folder rows rather than lumped into the root's, which would claim they
     * live at the top of the vault.
     */
    /* Band pitch is ADAPTIVE, between these two, and that is not a nicety.
     * Banding makes a layout taller in proportion to how many folders it spans —
     * this repo's own graph spans 14 — and a fixed airy pitch made the stack
     * several times the canvas height, so frameAll zoomed out far enough to
     * cross the `zoom > 0.7` threshold that hides node labels. The result was a
     * graph whose folder legend was perfectly readable and whose nodes were
     * anonymous dots: strictly worse than what it replaced. So the pitch shrinks
     * to fit the surface, down to a floor that still clears a node and the label
     * above it. A few bands stay airy; many get compact instead of illegible. */
    /* A band is a BLOCK, not a line, and it is as tall as it needs to be.
     *
     * One row per folder was the first attempt and it failed twice over, both
     * measured rather than guessed. Labels are drawn above their node and are
     * 60-100px wide against a REST of 135, so a nine-note folder on one line ran
     * its labels together — observed as "READMEauthoring", "catalogcomments".
     * And nine nodes in a row is ~1080 world px, which in a 320px side panel
     * fits only at a zoom far below the 0.7 threshold that hides node labels: a
     * graph of anonymous dots, strictly worse than the free layout it replaced,
     * which at least packed its nodes into a roughly square blob.
     *
     * So a band WRAPS. Its nodes are dealt across as many lanes as it takes for
     * the row to fit the surface, and the band grows to hold them. Both failures
     * fall out of the one mechanism: a wide window gives a folder a single airy
     * line, a narrow panel gives the same folder a compact labelled block, and
     * x-adjacent nodes are never in the same lane, so their labels cannot touch.
     * This is the file's existing instinct — what should vary per surface is how
     * the layout meets the canvas, not the spacing — applied to the lane count.
     *
     * LANE_GAP clears a node plus the 11px label above it. BAND_GAP is larger, so
     * the space BETWEEN two folders always reads as bigger than the space between
     * two lanes of one folder; that inequality is what keeps a band a band. */
    /* 38 rather than 30 because a label is drawn ABOVE its node (at
     * `y - radius - 4`) and so reaches up into the lane above it: at 30 the top
     * of a label and the bottom of the node one lane up overlapped by a few
     * pixels — visible in a screenshot as text grazing a dot. A lane has to clear
     * a node radius plus the label's own height, not just the label. */
    var LANE_GAP = 38;
    var BAND_GAP = 52;

    var LABEL_FONT = "11px -apple-system, BlinkMacSystemFont, sans-serif";

    /* In-row spacing is MEASURED, not assumed, and that is the last thing that
     * made labels unreadable. REST is 135, chosen when a label was guessed at
     * "60-100px" — but a note called `internal-requirements` renders ~150px wide,
     * so at 135 apart its label ran straight through its neighbour's: observed as
     * "READMEnal-requiremen". Guessing a wider constant only moves the threshold;
     * the width is knowable, so it is asked for. `spread` is the in-lane spacing
     * for the current payload and `widest` feeds frameAll's padding, because a
     * label overhangs its node's bounding box by half its width and the fit was
     * clipping the outermost ones off the canvas edge. */
    var spread = REST;
    var widest = 0;

    function measureLabels(list) {
      var ctx = canvas.getContext("2d");
      ctx.font = LABEL_FONT;
      var most = 0;
      for (var i = 0; i < list.length; i++) {
        // `measureText` is absent under the headless test harness, which is fine:
        // no measurement means `spread` stays REST and the layout is the one the
        // geometry tests already pin.
        var m = ctx.measureText ? ctx.measureText(list[i].label || "") : null;
        var w = (m && m.width) || 0;
        if (w > most) most = w;
      }
      return most;
    }
    var GUTTER = 92;          // screen px reserved on the left for band names
    // Keys for the two folderless bands. Leading slash so they cannot collide
    // with a real one whatever a folder is called: these keys are vault-RELATIVE
    // directories, which never begin with a separator.
    var GHOST_BAND = "/ghost";
    var TAG_BAND = "/tag";
    // `y` is a band's label centreline; `top` its first lane; `lanes`/`cols` how
    // its nodes are dealt. Keyed by band key.
    var bands = {
      order: [], y: Object.create(null), top: Object.create(null),
      lanes: Object.create(null), cols: Object.create(null),
      label: Object.create(null),
    };

    function bandKeyOf(node) {
      if (node.kind === "ghost") return GHOST_BAND;
      if (node.kind === "tag") return TAG_BAND;
      // graph.py sends `dir` outright. The id fallback is for a payload from
      // before it did — a note's id is its vault-relative path.
      if (typeof node.dir === "string") return node.dir;
      var slash = (node.id || "").lastIndexOf("/");
      return slash === -1 ? "" : node.id.slice(0, slash);
    }

    function bandLabelOf(key) {
      if (key === GHOST_BAND) return "unresolved";
      if (key === TAG_BAND) return "tags";
      return key === "" ? "root" : key + "/";
    }

    // [group, depth, name] — group puts the folderless bands last, depth puts
    // the root first, name settles ties.
    function bandRank(key) {
      if (key === GHOST_BAND) return [2, 0, ""];
      if (key === TAG_BAND) return [3, 0, ""];
      return [1, key === "" ? 0 : key.split("/").length, key];
    }

    /* The band table for one payload: which folders are present, in what order,
     * how many lanes each needs to fit the surface, and where each one sits. The
     * whole stack is centred vertically, so a single-band graph lands where the
     * old free layout put it. */
    function layoutBands(list, rect) {
      var seen = Object.create(null);
      var keys = [];
      var count = Object.create(null);
      var i, key;
      for (i = 0; i < list.length; i++) {
        key = bandKeyOf(list[i]);
        if (!seen[key]) { seen[key] = true; keys.push(key); }
        count[key] = (count[key] || 0) + 1;
      }
      keys.sort(function (a, b) {
        var ra = bandRank(a);
        var rb = bandRank(b);
        return ra[0] - rb[0] || ra[1] - rb[1]
          || (ra[2] < rb[2] ? -1 : ra[2] > rb[2] ? 1 : 0);
      });

      /* How wide a row may be, in world units. The layout is fitted by zoom, so
       * sizing the block to the canvas is what lands that zoom near 1 — which is
       * what keeps node labels above their visibility threshold. The gutter is
       * recomputed here rather than read from `gutterFor`, which reports on the
       * bands that are still being replaced. */
      var width = rect.width || 400;
      var usable = Math.max(2 * spread, width - Math.min(GUTTER, width * 0.28));

      var out = {
        order: keys, y: Object.create(null), top: Object.create(null),
        lanes: Object.create(null), cols: Object.create(null),
        label: Object.create(null),
      };
      var heights = [];
      var total = 0;
      for (i = 0; i < keys.length; i++) {
        key = keys[i];
        var lanes = Math.max(1, Math.ceil(count[key] * spread / usable));
        out.lanes[key] = lanes;
        out.cols[key] = Math.ceil(count[key] / lanes);
        out.label[key] = bandLabelOf(key);
        heights[i] = lanes * LANE_GAP;
        total += heights[i];
      }
      total += BAND_GAP * Math.max(0, keys.length - 1);

      var y = (rect.height || 400) / 2 - total / 2;
      for (i = 0; i < keys.length; i++) {
        key = keys[i];
        out.top[key] = y;
        out.y[key] = y + heights[i] / 2;   // the label's centreline
        y += heights[i] + BAND_GAP;
      }
      return out;
    }

    // The band names are drawn in screen space at the left edge, so the layout
    // is fitted into what is left. Only claimed when names are actually drawn,
    // and capped as a fraction of the width so a narrow sidebar is not mostly
    // gutter.
    function gutterFor(rect) {
      if (!bands.order.length) return 0;
      return Math.min(GUTTER, rect.width * 0.28);
    }

    function neighbours(node) {
      var set = {};
      set[node.id] = true;
      for (var i = 0; i < edges.length; i++) {
        if (edges[i].a === node) set[edges[i].b.id] = true;
        else if (edges[i].b === node) set[edges[i].a.id] = true;
      }
      return set;
    }

    function step() {
      var rect = canvas.getBoundingClientRect();
      var cx = rect.width / 2;
      var i, j;
      for (i = 0; i < nodes.length; i++) {
        var a = nodes[i];
        for (j = 0; j < nodes.length; j++) {
          if (i === j) continue;
          var b = nodes[j];
          /* Repulsion is horizontal, and only within one LANE — the row a node
           * actually shares. Two reasons, and the second was a real bug:
           *
           * Across BANDS it would be actively wrong, because two linked notes in
           * different folders should be free to sit one directly above the other,
           * and that column is the whole readability win.
           *
           * Across lanes of the SAME band it silently undid the wrap. Every node
           * in a folder repelling every other in x spreads them into one wide
           * row again no matter how they were seeded, so a nine-note folder
           * settled ~1080px wide, frameAll zoomed out to fit it, and the zoom
           * crossed the threshold that hides labels — the wrap computed
           * correctly and then dissolved over the next few hundred frames.
           * Confirmed by screenshot: bands named, nodes anonymous.
           *
           * Nodes in different lanes are already LANE_GAP apart vertically, which
           * is what their labels need; nothing has to be solved in x. */
          if (a.band !== b.band || a.lane !== b.lane) continue;
          var dx = a.x - b.x;
          var d2 = dx * dx;
          if (d2 < 0.01) { dx = Math.random() - 0.5; d2 = 0.01; }
          a.vx += (dx / Math.sqrt(d2)) * (REPULSION / d2);
        }
        a.vx += (cx - a.x) * 0.005;
      }
      for (i = 0; i < edges.length; i++) {
        var edge = edges[i];
        var ex = edge.b.x - edge.a.x;
        /* Three cases, and the middle one is a bug fix.
         *
         * Same LANE — side by side in one row, so hold them `spread` apart; both
         * need label room, exactly as the free layout did.
         *
         * Same band, DIFFERENT lane — no horizontal force at all. Lanes within a
         * band are an artifact of wrapping, not a layer of anything, so there is
         * nothing for an alignment pull to mean. Applying one (as this did) was
         * what collapsed a whole folder onto a single x: in a near-complete
         * folder the many cross-lane pulls overwhelm the in-lane repulsion, the
         * block converges to a column, and same-lane neighbours get squeezed to a
         * fraction of `spread` — labels overlapping again, from the opposite
         * direction. Their LANE_GAP of vertical offset is already their
         * separation.
         *
         * Different BAND — pull toward ex = 0. This is the only alignment that
         * carries meaning: a link descending from one folder to another reads as
         * a column, which is the point of banding at all. */
        var sameBand = edge.a.band === edge.b.band;
        var sameLane = sameBand && edge.a.lane === edge.b.lane;
        if (sameBand && !sameLane) continue;
        var target = sameLane ? spread : 0;
        var ed = Math.max(1, Math.abs(ex));
        var pull = (ed - target) * 0.02;
        edge.a.vx += (ex / ed) * pull;
        edge.b.vx -= (ex / ed) * pull;
      }
      for (i = 0; i < nodes.length; i++) {
        var n = nodes[i];
        if (n.pinned || n === drag) {
          n.vx = 0;
        } else {
          // Speed ceiling. The repulsion sum grows with node count, and velocity
          // accumulates across steps (damped only after the move), so a folder of
          // hundreds threw nodes thousands of pixels apart in the first few frames
          // and then cooled before it could come back — a graph that "flies apart"
          // and lands mostly off-screen. Small graphs never reach this.
          var speed = Math.abs(n.vx);
          if (speed > MAX_SPEED) n.vx = (n.vx / speed) * MAX_SPEED;
          n.x += n.vx * alpha;
          n.vx *= 0.82;
        }
        /* y is the layout's answer, not the sim's. Eased rather than snapped so
         * that a note whose folder changed glides to its new band instead of
         * teleporting. A node the user dragged keeps the y they put it at —
         * `pinned` alone cannot stand in for that, because the focus node is
         * pinned by setData and must still sit in its own band. */
        if (n !== drag && !n.userPinned) n.y += (n.bandY - n.y) * 0.2;
        n.vy = 0;
      }
      alpha *= 0.99;
    }

    function draw() {
      if (dead) return;
      var rect = canvas.getBoundingClientRect();
      var dpr = window.devicePixelRatio || 1;
      if (canvas.width !== Math.round(rect.width * dpr)) {
        canvas.width = Math.round(rect.width * dpr);
        canvas.height = Math.round(rect.height * dpr);
      }
      var ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, rect.width, rect.height);

      var accent = token("--accent");
      var muted = token("--fg-muted");
      var ghost = token("--ghost");
      // Edges read from --ghost, not --border: a border token is tuned to be
      // barely there against its own background (Obsidian's is base-30, ~1.5:1),
      // and an edge that cannot be seen is not an edge. --ghost is the faint TEXT
      // token — recessive but legible, in both themes.
      var line = token("--ghost");
      var near = hover ? neighbours(hover) : null;
      var i;

      /* The band scaffolding is drawn in SCREEN space, and deliberately not in
       * world space: a name that scaled with the zoom would be unreadable on a
       * fitted folder graph and would slide off the left edge on any pan, and
       * these names are the axis legend — if they are gone, the vertical axis
       * means nothing again.
       *
       * The NAME is drawn even for a single band; only the separators need more
       * than one. "Every note here is in widgets/specs/" is the answer to the
       * question this layout exists to answer, and withholding it exactly when
       * the neighbourhood happens to be one folder deep is withholding it in the
       * common case. */
      var named = bands.order.length > 0;
      var multi = bands.order.length > 1;
      if (multi) {
        ctx.save();
        ctx.strokeStyle = line;
        ctx.lineWidth = 1;
        ctx.globalAlpha = 0.3;
        for (i = 1; i < bands.order.length; i++) {
          // One separator between each adjacent pair, in the empty BAND_GAP —
          // never above the first band or below the last, where a lone rule
          // would read as a boundary with nothing on the far side of it.
          var cut = Math.round((bands.top[bands.order[i]] - BAND_GAP / 2) * zoom
                               + oy) + 0.5;
          if (cut < 0 || cut > rect.height) continue;
          ctx.beginPath();
          ctx.moveTo(0, cut);
          ctx.lineTo(rect.width, cut);
          ctx.stroke();
        }
        ctx.restore();
      }

      ctx.save();
      ctx.translate(ox, oy);
      ctx.scale(zoom, zoom);

      /* Edge weight falls away as the graph gets denser, and this is the fix for
       * the second thing that made the picture unreadable. A folder whose notes
       * all cross-reference each other is near-complete — nine notes carried
       * ~30 edges — and drawn at uniform weight that is a hairball: the lines
       * add up to more ink than the nodes, so the eye finds structure in the
       * crossings rather than in the layout. No routing or bundling scheme fixes
       * that, because the honest content of a near-complete graph is "these all
       * link to each other", which no arrangement of 30 curves states better
       * than a faint texture does.
       *
       * So: the more edges per node, the fainter they go. Hover is what makes an
       * individual edge legible again — `near` already lights a neighbourhood,
       * and against a recessive field that highlight now actually reads. The
       * floor keeps a sparse graph looking exactly as it did. */
      var density = edges.length / Math.max(1, nodes.length);
      var wash = Math.max(0.22, Math.min(1, 1.5 / Math.max(1, density)));
      ctx.lineWidth = 1 / zoom;
      for (i = 0; i < edges.length; i++) {
        var edge = edges[i];
        var lit = near && near[edge.a.id] && near[edge.b.id];
        // A lit edge is always full strength; only the resting field is washed
        // out, and when anything is hovered the rest recedes further still.
        ctx.globalAlpha = lit ? 1 : (near ? wash * 0.5 : wash);
        ctx.strokeStyle = lit ? accent : line;
        ctx.setLineDash(
          edge.a.kind === "ghost" || edge.b.kind === "ghost" ? [3, 3] : []);
        ctx.beginPath();
        ctx.moveTo(edge.a.x, edge.a.y);
        ctx.lineTo(edge.b.x, edge.b.y);
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
      ctx.setLineDash([]);

      var labels = zoom > 0.7;
      // The same font `measureLabels` measured with — a drift between the two
      // would silently reintroduce the overlap that spacing exists to prevent.
      ctx.font = LABEL_FONT;
      ctx.textAlign = "center";
      for (i = 0; i < nodes.length; i++) {
        var node = nodes[i];
        var inFocus = !near || near[node.id];
        ctx.globalAlpha = inFocus ? 1 : 0.25;
        ctx.fillStyle = node.kind === "ghost" ? ghost
          : (node.focus || node.kind === "tag") ? accent : muted;
        ctx.beginPath();
        ctx.arc(node.x, node.y, radius(node), 0, Math.PI * 2);
        ctx.fill();
        if (labels || (near && near[node.id])) {
          ctx.fillStyle = node.kind === "ghost" ? ghost : muted;
          ctx.fillText(node.label, node.x, node.y - radius(node) - 4);
        }
        ctx.globalAlpha = 1;
      }
      ctx.restore();

      // Band names last, so no edge or node is drawn over the legend.
      if (named) {
        ctx.save();
        ctx.font = "10px -apple-system, BlinkMacSystemFont, sans-serif";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        ctx.fillStyle = muted;
        ctx.globalAlpha = 0.8;
        for (i = 0; i < bands.order.length; i++) {
          var key = bands.order[i];
          var ly = bands.y[key] * zoom + oy;
          if (ly < 6 || ly > rect.height - 6) continue;
          ctx.fillText(bands.label[key], 8, ly);
        }
        ctx.restore();
      }
    }

    /* Fit the whole graph to the canvas while the sim is still settling.
     * The layout above is sized for READABILITY, not for the surface it lands on
     * — so without this a comfortably-spaced neighbourhood would sit half outside
     * a narrow sidebar, and a folder of hundreds would run off the window. It is
     * also what makes the same spacing serve both surfaces: what differs between
     * a 320px panel and a full window is the zoom, not the layout.
     *
     * Bounded both ways (0.15 to 1.5, so a two-node graph is not blown up and a
     * huge one still resolves to something), and it stops the moment the user
     * pans, zooms or drags — their framing is never fought over. Deliberately not
     * reset by setData: every autosave re-sends the graph (MD-9), and resetting
     * would yank the view out from under someone mid-read. */
    function frameAll() {
      if (userFramed || !nodes.length) return;
      var rect = canvas.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
      for (var i = 0; i < nodes.length; i++) {
        minX = Math.min(minX, nodes[i].x);
        maxX = Math.max(maxX, nodes[i].x);
        minY = Math.min(minY, nodes[i].y);
        maxY = Math.max(maxY, nodes[i].y);
      }
      /* Padding leaves room for a label, which is drawn above the node and
       * CENTRED on it, so it overhangs the bounding box by half its width either
       * side. A flat 32 clipped real labels off the canvas edge — but reserving
       * the full half-width is worse, and measurably so: in a 330px panel a
       * ~130px label demands 130px of margin against a block only 200px wide, so
       * the fit zooms out to 0.6, crosses the label threshold, and hides EVERY
       * label to avoid clipping ONE. The allowance is therefore capped. The
       * accepted cost is that the outermost long label can run past the edge;
       * the alternative is a canvas of anonymous dots, and panning is right
       * there. */
      var pad = 32;
      var padX = pad + Math.min(widest / 2, 28);
      // The band names occupy the left edge in screen space, so the graph is
      // fitted into what remains and centred there — otherwise a fitted layout
      // is centred on the whole canvas and its leftmost nodes sit under the
      // legend.
      var gutter = gutterFor(rect);
      var usable = Math.max(40, rect.width - gutter);
      zoom = Math.min(1.5, Math.max(0.15, Math.min(
        usable / (maxX - minX + padX * 2),
        rect.height / (maxY - minY + pad * 2))));
      ox = gutter + usable / 2 - ((minX + maxX) / 2) * zoom;
      oy = rect.height / 2 - ((minY + maxY) / 2) * zoom;
    }

    function run() {
      if (frame || dead) return;
      frame = requestAnimationFrame(function tick() {
        frame = null;
        if (dead) return;
        if (alpha > 0.02 || drag) {
          step();
          frameAll();
          frame = requestAnimationFrame(tick);
        }
        draw();
      });
    }

    function setData(payload) {
      var rect = canvas.getBoundingClientRect();
      var cx = rect.width / 2 || 160;
      var byId = {};
      /* Positions are CARRIED OVER for ids we already have. Load-bearing, and
       * the obvious "simplification" (map every node onto the spiral, alpha = 1,
       * done) is what this replaced: every autosave re-sends the graph (MD-9),
       * so with a full re-seed the layout restarted every 2 seconds while you
       * typed — the same "yank the view out from under someone mid-read"
       * frameAll refuses to do, arriving by another door. Velocity and the pin
       * come across too: a node you dragged into place stays put, and one still
       * settling does not lose its momentum mid-flight. */
      var previous = {};
      var previousCount = nodes.length;
      for (var p = 0; p < nodes.length; p++) previous[nodes[p].id] = nodes[p];
      var fresh = 0;   // ids we have never laid out
      /* Bands are recomputed per payload, before anything is seeded: a node's y
       * is its band's centreline, so the table has to exist first. The whole
       * spiral seeding this replaced is gone with the free layout — y is no
       * longer the sim's to choose, so there is nothing to seed in it. */
      /* Measured BEFORE the bands, because the lane count is derived from the
       * in-row spacing and the spacing is derived from the widest label. */
      widest = measureLabels(payload.nodes || []);
      spread = Math.max(REST, widest + 20);
      var next = layoutBands(payload.nodes || [], rect);
      /* x is seeded as a tidy row per band, centred on the canvas. Spacing at
       * REST means the row starts at roughly the density the sim wants, which is
       * the same reason the old disc seeding existed: a band of hundreds all
       * stacked on one x would need repulsion enough to blow the row apart
       * faster than it could cool. */
      var slot = Object.create(null);
      var total = Object.create(null);
      var keyOf = [];
      var list = payload.nodes || [];
      for (var q = 0; q < list.length; q++) {
        keyOf[q] = bandKeyOf(list[q]);
        total[keyOf[q]] = (total[keyOf[q]] || 0) + 1;
      }
      nodes = list.map(function (node, i) {
        var key = keyOf[i];
        var at = slot[key] = (slot[key] === undefined ? 0 : slot[key] + 1);
        /* Deal round-robin: lane cycles fastest, so consecutive `at` — which are
         * x-NEIGHBOURS, since the column advances only after a full cycle — are
         * never in the same lane. That is precisely the property that stops two
         * side-by-side labels from overlapping. */
        var lanes = next.lanes[key];
        var lane = at % lanes;
        var col = Math.floor(at / lanes);
        var bandY = next.top[key] + (lane + 0.5) * LANE_GAP;
        var focused = node.id === payload.focus;
        var kept = previous[node.id];
        if (!kept) fresh++;
        var seeded = {
          id: node.id, kind: node.kind, label: node.label,
          // `label` is a DISPLAY string (a note's is its filename); `target` is
          // the authored link target a ghost was made from. Creating a note is a
          // path operation, so it reads target and never label.
          path: node.path, target: node.target, degree: node.degree,
          band: key, lane: lane, bandY: bandY,
          x: kept ? kept.x : (focused ? cx
            : cx + (col - (next.cols[key] - 1) / 2) * spread),
          /* y comes from the band, even for a node carried over — a note that
           * moved folder belongs in its new row, and the ease in step() gets it
           * there. The one exception is a node the user dragged: their placement
           * is theirs to keep, on both axes. */
          y: kept && kept.userPinned ? kept.y : bandY,
          vx: kept ? kept.vx : 0, vy: 0,
          // The focus is pinned horizontally at the centre: a local graph is
          // *about* one note, so letting the sim carry it away loses the point.
          // A pin the user set by dragging survives a re-send for the same
          // reason, and `userPinned` is what distinguishes the two.
          pinned: focused || (kept ? kept.pinned : false),
          userPinned: kept ? kept.userPinned : false,
          focus: focused,
        };
        byId[node.id] = seeded;
        return seeded;
      });
      bands = next;
      edges = (payload.edges || []).map(function (edge) {
        return { kind: edge.kind, a: byId[edge.source], b: byId[edge.target] };
      }).filter(function (edge) { return edge.a && edge.b; });
      /* How hard to re-anneal, in proportion to what actually changed. A first
       * payload gets the full run. A re-send that added or dropped nodes gets a
       * partial one — enough for the newcomers to find room, not enough to throw
       * the settled layout away. A re-send with the same node set gets none at
       * all: `run()` still draws once (it draws whether or not it steps), which
       * is all a label or degree change needs. */
      if (!previousCount) alpha = 1;
      else if (fresh || nodes.length !== previousCount) alpha = Math.max(alpha, 0.3);
      run();
    }

    function at(event) {
      var rect = canvas.getBoundingClientRect();
      var x = (event.clientX - rect.left - ox) / zoom;
      var y = (event.clientY - rect.top - oy) / zoom;
      var best = null;
      var bestDistance = Infinity;
      for (var i = 0; i < nodes.length; i++) {
        var node = nodes[i];
        var d = Math.sqrt(
          (node.x - x) * (node.x - x) + (node.y - y) * (node.y - y));
        if (d < radius(node) + 6 && d < bestDistance) { best = node; bestDistance = d; }
      }
      return { node: best, x: x, y: y };
    }

    function onMove(event) {
      var found = at(event);
      if ((drag || pan) && !moved) {
        var tx = event.clientX - pressX;
        var ty = event.clientY - pressY;
        if (tx * tx + ty * ty > DRAG_SLOP * DRAG_SLOP) moved = true;
      }
      if (drag) {
        drag.x = found.x;
        drag.y = found.y;
        run();
        return;
      }
      if (pan) {
        ox += event.clientX - pan.x;
        oy += event.clientY - pan.y;
        pan = { x: event.clientX, y: event.clientY };
        draw();
        return;
      }
      if (found.node !== hover) {
        hover = found.node;
        canvas.style.cursor = found.node ? "pointer" : "grab";
        draw();
      }
    }

    function onDown(event) {
      var found = at(event);
      userFramed = true; // hands on: stop auto-framing from here on
      pressX = event.clientX;
      pressY = event.clientY;
      moved = false;
      if (found.node) {
        drag = found.node;
        drag.pinned = true;     // drag-to-pin
        // Drag-to-pin takes the y too: past this point the band no longer pulls
        // this node, so a node lifted out of its row to be read stays lifted.
        drag.userPinned = true;
      } else {
        pan = { x: event.clientX, y: event.clientY };
      }
    }

    function onUp() { drag = null; pan = null; }

    function onClick(event) {
      if (moved) return; // that was a drag or a pan; see DRAG_SLOP
      var found = at(event);
      if (!found.node) return;
      if (found.node.kind === "note" && found.node.path) onOpenNote(found.node.path);
      else if (found.node.kind === "ghost" && found.node.target) {
        onCreateGhost(found.node.target);
      }
    }

    function onWheel(event) {
      event.preventDefault();
      userFramed = true;
      var rect = canvas.getBoundingClientRect();
      var px = event.clientX - rect.left;
      var py = event.clientY - rect.top;
      var next = Math.min(4, Math.max(0.25, zoom * Math.exp(-event.deltaY * 0.002)));
      // Zoom about the pointer, so the node under the cursor stays under it.
      ox = px - ((px - ox) / zoom) * next;
      oy = py - ((py - oy) / zoom) * next;
      zoom = next;
      draw();
    }

    function onResize() { alpha = Math.max(alpha, 0.4); run(); }

    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("mousedown", onDown);
    canvas.addEventListener("click", onClick);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    window.addEventListener("mouseup", onUp);
    window.addEventListener("resize", onResize);
    var themeWatcher = new MutationObserver(function () { draw(); });
    themeWatcher.observe(document.documentElement,
      { attributes: true, attributeFilter: ["data-theme"] });

    return {
      setData: setData,
      draw: draw,
      nudge: onResize,
      destroy: function () {
        dead = true;
        if (frame) cancelAnimationFrame(frame);
        canvas.removeEventListener("mousemove", onMove);
        canvas.removeEventListener("mousedown", onDown);
        canvas.removeEventListener("click", onClick);
        canvas.removeEventListener("wheel", onWheel);
        window.removeEventListener("mouseup", onUp);
        window.removeEventListener("resize", onResize);
        themeWatcher.disconnect();
      },
    };
  }

  window.fusedGraph = { create: create };
})();
