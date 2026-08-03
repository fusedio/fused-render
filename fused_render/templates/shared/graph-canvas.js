/* Link-graph canvas — shared by the markdown template's local panel and the
 * folder-level `graph` mode (SPEC §32, MD-19). Extracted at the point the
 * second surface appeared, so there is one layout and one set of interaction
 * rules rather than two that drift; served from the /template-shared/ mount
 * (see server.py) like ro-badge.js and sciviz.mjs.
 *
 * Load:  <script src="/template-shared/graph-canvas.js"></script>
 * Use:   const g = fusedGraph.create({ canvas, onOpenNote, onCreateGhost });
 *        g.setData(payload);   // graph.py's `graph` action, verbatim
 *        g.destroy();
 *
 * The layout is LAYERED BY FOLDER, which is the one place this deliberately
 * stops copying Obsidian (D163). A free force layout answers "what links to
 * what" and nothing else, and the feedback on it was that the picture was
 * unreadable: with every node free in both axes, position carried no meaning.
 * Here the vertical axis is spent on something: one horizontal band per
 * folder, ordered shallowest-first, so a node's height IS its place in the
 * tree and a chain of links reads as a descent through it.
 *
 * Positions are ASSIGNED, not simulated, and that replaced a spring sim after
 * two rounds of screenshots showed why a sim cannot serve this layout. The
 * sim ran two forces that want opposite things: an in-lane spring holding
 * neighbours a label-width apart, and a cross-band pull aligning linked notes
 * into columns. In the graph this panel actually shows — a hub note that
 * everything links to — the column pulls all point at the same x, they
 * outnumber the springs, and the springs lose: labels compressed into
 * "configuratiobservability" while every edge converged on one central rope
 * of overlapping lines. No constant fixes a fight between two forces; the
 * fix is to stop fighting. Within a band, nodes are ORDERED by where their
 * neighbours sit (a barycenter sweep, the standard Sugiyama move, so links
 * run as near-vertical as the ordering allows) and then SPACED by their own
 * measured label widths (so two labels cannot touch, by construction).
 * Nothing is left for a force to solve. Nodes glide to their assigned spot,
 * which keeps re-sends calm: every autosave re-sends the graph (MD-9), and a
 * layout that restarts under the reader was the first thing this file ever
 * had to fix.
 *
 * Behaviours kept from Obsidian: the layout is fitted to the canvas, node
 * radius scales with degree, labels fade out past a zoom threshold, hovering
 * lights the neighbourhood, dragging pins a node, ghost nodes are dim with
 * dashed edges, clicking a node opens it. Colours are read from CSS custom
 * properties AT DRAW TIME, because var() cannot resolve inside a canvas
 * fillStyle — so a theme flip redraws rather than repainting nothing (SPEC
 * §30's rule for JS-held colours).
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
    var frame = null;
    var dead = false;
    var settling = false;
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

    /* ---- the grid ------------------------------------------------------------
     * LANE_GAP clears a node plus the 11px label drawn above it — at less, a
     * label's ascenders grazed the dot one lane up in screenshots. BAND_GAP is
     * smaller than it was under the sim (52) because bands now carry their own
     * alternating fill, so the gap no longer has to do all the work of saying
     * "different folder"; what it must still do is stay visibly LARGER than
     * LANE_GAP, so two lanes of one folder always sit closer than two folders.
     *
     * SLOT_PAD is the air between two labels in one lane; SLOT_MIN is the slot
     * of an unmeasured label (the headless test harness has no measureText),
     * which also keeps a one-letter note from producing a sliver column. */
    var LANE_GAP = 40;
    var BAND_GAP = 48;
    var SLOT_PAD = 18;
    var SLOT_MIN = 60;

    var LABEL_FONT = "11px -apple-system, BlinkMacSystemFont, sans-serif";
    var GUTTER = 92;          // screen px reserved on the left for band names

    /* The widest label feeds frameAll's padding: a label overhangs its node's
     * bounding box by half its width, and the fit was clipping the outermost
     * ones off the canvas edge. */
    var widest = 0;

    function measureLabels(list) {
      var ctx = canvas.getContext("2d");
      ctx.font = LABEL_FONT;
      for (var i = 0; i < list.length; i++) {
        // `measureText` is absent under the headless test harness, which is
        // fine: an unmeasured label gets the SLOT_MIN slot, and the geometry
        // tests pin that layout.
        var m = ctx.measureText ? ctx.measureText(list[i].label || "") : null;
        list[i]._w = (m && m.width) || 0;
        if (list[i]._w > widest) widest = list[i]._w;
      }
    }

    // Key for the one folderless band. Leading slash so it cannot collide with
    // a real one whatever a folder is called: these keys are vault-RELATIVE
    // directories, which never begin with a separator.
    var GHOST_BAND = "/ghost";
    // `top`/`height` bound a band's fill; `y` centres its name. Keyed by band key.
    var bands = {
      order: [], y: Object.create(null), top: Object.create(null),
      height: Object.create(null), label: Object.create(null),
    };

    function bandKeyOf(node) {
      if (node.kind === "ghost") return GHOST_BAND;
      // graph.py sends `dir` outright. The id fallback is for a payload from
      // before it did — a note's id is its vault-relative path.
      if (typeof node.dir === "string") return node.dir;
      var slash = (node.id || "").lastIndexOf("/");
      return slash === -1 ? "" : node.id.slice(0, slash);
    }

    function bandLabelOf(key) {
      if (key === GHOST_BAND) return "unresolved";
      return key === "" ? "root" : key + "/";
    }

    // [group, depth, name] — group puts the folderless band last, depth puts
    // the root first, name settles ties.
    function bandRank(key) {
      if (key === GHOST_BAND) return [2, 0, ""];
      return [1, key === "" ? 0 : key.split("/").length, key];
    }

    /* ---- the layout ----------------------------------------------------------
     * Everything below computes each node's TARGET (tx, ty); nothing here moves
     * a node. step() glides them there.
     *
     * Per band: order nodes by the mean x of their neighbours (three
     * alternating sweeps, which is where the crossings go away — a note linked
     * from the left half of the band above lands in the left half of its own),
     * wrap the ordered row into as many lanes as the surface needs, and hand
     * out x by cumulative slot widths, so each label owns exactly the room it
     * measured plus SLOT_PAD. Dealing is round-robin (lane cycles fastest), so
     * x-adjacent slots are never in the same lane — a second, independent
     * guarantee against label contact on top of the measured slots.
     *
     * The focus note is centred by shifting the WHOLE layout, not by pinning
     * it against its band-mates: a local graph is about one note, and the
     * barycenter sweeps then gather its neighbourhood around that centre. */
    function layout(list, links, rect) {
      var byBand = Object.create(null);
      var keys = [];
      var i, key;
      for (i = 0; i < list.length; i++) {
        key = bandKeyOf(list[i]);
        if (!byBand[key]) { byBand[key] = []; keys.push(key); }
        byBand[key].push(list[i]);
      }
      keys.sort(function (a, b) {
        var ra = bandRank(a);
        var rb = bandRank(b);
        return ra[0] - rb[0] || ra[1] - rb[1]
          || (ra[2] < rb[2] ? -1 : ra[2] > rb[2] ? 1 : 0);
      });

      // Adjacency once, by node object. Ghost edges count the same as real
      // ones: an unresolved link still says where its ghost should sit.
      var peers = new Map();
      for (i = 0; i < links.length; i++) {
        var a = links[i].a, b = links[i].b;
        if (!peers.has(a)) peers.set(a, []);
        if (!peers.has(b)) peers.set(b, []);
        peers.get(a).push(b);
        peers.get(b).push(a);
      }

      /* How wide a lane may be, in world units. The layout is fitted by zoom,
       * so sizing the block to the canvas is what lands that zoom near 1 —
       * which is what keeps node labels above their visibility threshold. */
      var width = rect.width || 400;
      // The gutter is only reserved when band names will actually be drawn —
      // one band means no legend (see the draw pass), so a single-folder
      // neighbourhood gets the full width to lay out in.
      var gutterPx = keys.length > 1 ? Math.min(GUTTER, width * 0.28) : 0;
      var usable = Math.max(2 * SLOT_MIN, width - gutterPx);

      function slotOf(node) {
        return Math.max(SLOT_MIN, (node._w || 0) + SLOT_PAD);
      }

      /* Deal one band's ordered nodes into a slot grid and assign tx/ty.
       * Column widths are the widest slot the column holds across its lanes,
       * so a long label widens its own column and nothing else. */
      function place(key, top) {
        var row = byBand[key];
        var lanes = 1;
        var total = 0;
        for (var n = 0; n < row.length; n++) total += slotOf(row[n]);
        /* Never more lanes than nodes. `ceil(total / usable)` answers "how many
         * lanes would the slots need", which a band of two very long labels can
         * push above its own node count — and since the band's height is
         * `lanes * LANE_GAP`, the surplus lanes are EMPTY: the band grows,
         * every band below it is pushed down, and frameAll zooms the whole
         * graph out to fit space nothing is drawn in. */
        lanes = Math.max(1, Math.min(row.length, Math.ceil(total / usable)));
        var cols = Math.ceil(row.length / lanes);
        var colW = [];
        for (n = 0; n < row.length; n++) {
          var c = Math.floor(n / lanes);
          colW[c] = Math.max(colW[c] || 0, slotOf(row[n]));
        }
        var colX = [];
        var x = 0;
        for (c = 0; c < cols; c++) { colX[c] = x + colW[c] / 2; x += colW[c]; }
        for (n = 0; n < row.length; n++) {
          c = Math.floor(n / lanes);
          row[n].tx = colX[c] - x / 2;
          row[n].ty = top + ((n % lanes) + 0.5) * LANE_GAP;
        }
        return lanes * LANE_GAP;
      }

      // First pass: alphabetical order (stable across payloads for a node set
      // with no links at all), provisional coordinates.
      var out = {
        order: keys, y: Object.create(null), top: Object.create(null),
        height: Object.create(null), label: Object.create(null),
      };
      var y = 0;
      for (i = 0; i < keys.length; i++) {
        key = keys[i];
        byBand[key].sort(function (m, n) {
          return m.label < n.label ? -1 : m.label > n.label ? 1 : 0;
        });
        out.top[key] = y;
        out.label[key] = bandLabelOf(key);
        out.height[key] = place(key, y);
        out.y[key] = y + out.height[key] / 2;
        y += out.height[key] + BAND_GAP;
      }

      /* Barycenter sweeps: re-order each band by where each node's neighbours
       * currently sit, then re-place. Alternating direction is the textbook
       * refinement — a top-down pass orders each band by the settled bands
       * above it, the bottom-up pass feeds that order back the other way.
       * Three sweeps is where improvement stops being visible on graphs this
       * size; this is a panel, not a compiler. Nodes with no neighbours keep
       * their relative (alphabetical) order — the sort key falls back to the
       * node's own position, and the sort is stable. */
      for (var sweep = 0; sweep < 3; sweep++) {
        var ordered = sweep % 2 ? keys.slice().reverse() : keys;
        for (i = 0; i < ordered.length; i++) {
          key = ordered[i];
          var row = byBand[key];
          for (var n = 0; n < row.length; n++) {
            var near = peers.get(row[n]);
            row[n]._link = !!(near && near.length);
            if (!row[n]._link) { row[n]._bary = row[n].tx; continue; }
            var sum = 0;
            for (var p = 0; p < near.length; p++) sum += near[p].tx;
            row[n]._bary = sum / near.length;
          }
          /* An unlinked node's key is its own position — it has no opinion.
           * That makes exact TIES against a linked node common (slots are a
           * symmetric grid), and a stable sort then strands the linked node
           * away from its neighbours on every sweep. The tie goes to the
           * linked node: its position is the one carrying information. */
          row.sort(function (m, o) {
            return (m._bary - o._bary) || ((o._link ? 1 : 0) - (m._link ? 1 : 0));
          });
          place(key, out.top[key]);
        }
      }

      /* Centre on the focus, if there is one: the whole layout shifts so the
       * focus note sits at the canvas mid-line, and everything the sweeps
       * gathered around it comes along. Without a focus (the folder mode) each
       * band centres itself — place() already put every band's mean at 0, so
       * the plain cx shift lands them all on the same mid-line. */
      var focus = null;
      for (i = 0; i < list.length; i++) if (list[i].focus) focus = list[i];
      var cx = (rect.width || 400) / 2;
      var shift = cx - (focus ? focus.tx : 0);
      for (i = 0; i < list.length; i++) list[i].tx += shift;

      /* The stack is centred vertically on the canvas, so a single-band graph
       * lands where a free layout would have put it. `y` accumulated one
       * BAND_GAP past the last band — trim it, or everything centres a
       * half-gap high. */
      if (keys.length) y -= BAND_GAP;
      var lift = (rect.height || 400) / 2 - y / 2;
      for (i = 0; i < keys.length; i++) {
        key = keys[i];
        out.top[key] += lift;
        out.y[key] += lift;
      }
      for (i = 0; i < list.length; i++) list[i].ty += lift;
      return out;
    }

    // The band names are drawn in screen space at the left edge, so the layout
    // is fitted into what is left. Only claimed when names are actually drawn,
    // and capped as a fraction of the width so a narrow sidebar is not mostly
    // gutter.
    function gutterFor(rect) {
      if (bands.order.length < 2) return 0;
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

    /* Glide every node toward its assigned spot. The 0.5px snap matters: an
     * asymptotic ease leaves nodes at 209.99… forever, which keeps the frame
     * loop alive and off-grid. A node the user dragged keeps the position they
     * gave it, on both axes — their placement is theirs to keep. */
    function step() {
      settling = false;
      for (var i = 0; i < nodes.length; i++) {
        var n = nodes[i];
        if (n === drag || n.userPinned) continue;
        var dx = n.tx - n.x;
        var dy = n.ty - n.y;
        if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) { n.x = n.tx; n.y = n.ty; continue; }
        n.x += dx * 0.2;
        n.y += dy * 0.2;
        settling = true;
      }
    }

    /* One edge, as a vertical S-curve: straight out of both nodes, bending in
     * the middle. Cross-band edges are the common case and near-vertical is
     * their honest shape — the curve keeps two edges into one hub visually
     * separable where straight lines fused into a rope. A same-band edge
     * (a.y === b.y gives a degenerate S) bows downward instead. */
    function edgePath(ctx, a, b) {
      ctx.moveTo(a.x, a.y);
      if (Math.abs(a.y - b.y) < LANE_GAP / 2) {
        var bow = Math.min(24, Math.abs(a.x - b.x) / 4) + 6;
        ctx.quadraticCurveTo((a.x + b.x) / 2, Math.max(a.y, b.y) + bow, b.x, b.y);
      } else {
        var my = (a.y + b.y) / 2;
        ctx.bezierCurveTo(a.x, my, b.x, my, b.x, b.y);
      }
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
      var fg = token("--fg");
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
       * means nothing again. Alternate bands carry a whisper of the foreground
       * colour rather than a separator line between them: a fill says "this
       * whole strip is one folder" everywhere the strip is, where the lone
       * hairlines read as stray edges once real edges curve past them.
       *
       * Fills AND names only when there is more than one band. With a single
       * band the vertical axis distinguishes nothing, so the legend is one
       * folder name repeated down the left edge — it labels the whole panel, not
       * a band within it, and the breadcrumb already says where you are. Drop
       * it and the gutter it reserved (gutterFor), and the graph gets the width
       * back. */
      var named = bands.order.length > 1;
      if (bands.order.length > 1) {
        ctx.save();
        ctx.fillStyle = fg || "#888";
        for (i = 0; i < bands.order.length; i += 2) {
          var key = bands.order[i];
          var bt = (bands.top[key] - BAND_GAP / 2) * zoom + oy;
          var bh = (bands.height[key] + BAND_GAP) * zoom;
          if (bt > rect.height || bt + bh < 0) continue;
          ctx.globalAlpha = 0.04;
          ctx.fillRect(0, bt, rect.width, bh);
        }
        ctx.restore();
      }

      ctx.save();
      ctx.translate(ox, oy);
      ctx.scale(zoom, zoom);

      /* Edge weight falls away as the graph gets denser. A folder whose notes
       * all cross-reference each other is near-complete — nine notes carried
       * ~30 edges — and drawn at uniform weight that is a hairball: the lines
       * add up to more ink than the nodes, so the eye finds structure in the
       * crossings rather than in the layout. The honest content of a
       * near-complete graph is "these all link to each other", which a faint
       * texture states better than 30 competing curves do.
       *
       * Two things stay legible against the wash: the hovered neighbourhood
       * (accent, full strength), and the FOCUS note's own edges — the panel
       * exists to show one note's place, so the links that are actually its
       * own always read a step above the field. */
      var density = edges.length / Math.max(1, nodes.length);
      var wash = Math.max(0.18, Math.min(0.65, 1.2 / Math.max(1, density)));
      ctx.lineWidth = 1 / zoom;
      for (i = 0; i < edges.length; i++) {
        var edge = edges[i];
        var lit = near && near[edge.a.id] && near[edge.b.id];
        var own = edge.a.focus || edge.b.focus;
        ctx.globalAlpha = lit ? 1
          : near ? wash * 0.4
          : own ? Math.min(1, wash * 2.2)
          : wash;
        ctx.strokeStyle = lit ? accent : line;
        ctx.setLineDash(
          edge.a.kind === "ghost" || edge.b.kind === "ghost" ? [3, 3] : []);
        ctx.beginPath();
        edgePath(ctx, edge.a, edge.b);
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
      ctx.setLineDash([]);

      var labels = zoom > 0.7;
      /* Which labels actually print is decided per FRAME, against what is
       * already printed. The slots guarantee two settled labels cannot touch,
       * but the guarantee only covers what the layout placed: a node the user
       * dragged onto a neighbour, or two labels passing mid-glide, can still
       * collide on screen. Rather than draw the collision, the lower-priority
       * label is dropped for the frame — focus first, then the hovered
       * neighbourhood, then bigger nodes — and hover is the way to summon a
       * dropped one back. A label half-read is worse than a dot: the dot at
       * least says "hover me". */
      var show = Object.create(null);
      var kept = [];
      if (nodes.length) {
        var order = nodes.slice().sort(function (m, n) {
          var pm = (m.focus ? 4 : 0) + (near && near[m.id] ? 2 : 0);
          var pn = (n.focus ? 4 : 0) + (near && near[n.id] ? 2 : 0);
          return (pn - pm) || (n.degree - m.degree);
        });
        for (i = 0; i < order.length; i++) {
          var cand = order[i];
          if (!(labels || (near && near[cand.id]))) continue;
          var w = (cand._w || 0) + 6;
          var box = { l: cand.x - w / 2, r: cand.x + w / 2,
                      t: cand.y - radius(cand) - 16, b: cand.y - radius(cand) - 2 };
          var clear = true;
          for (var k = 0; k < kept.length; k++) {
            var o = kept[k];
            if (box.l < o.r && box.r > o.l && box.t < o.b && box.b > o.t) {
              clear = false;
              break;
            }
          }
          if (clear) { kept.push(box); show[cand.id] = true; }
        }
      }
      // The same font `measureLabels` measured with — a drift between the two
      // would silently reintroduce the overlap that the slots exist to prevent.
      ctx.font = LABEL_FONT;
      ctx.textAlign = "center";
      for (i = 0; i < nodes.length; i++) {
        var node = nodes[i];
        var inFocus = !near || near[node.id];
        ctx.globalAlpha = inFocus ? 1 : 0.25;
        ctx.fillStyle = node.kind === "ghost" ? ghost
          : node.focus ? accent : muted;
        ctx.beginPath();
        ctx.arc(node.x, node.y, radius(node), 0, Math.PI * 2);
        ctx.fill();
        if (show[node.id]) {
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
        // Two band names cannot collide: adjacent centrelines are at least
        // LANE_GAP + BAND_GAP apart in world units, and both zoom clamps
        // (frameAll's 0.15 floor, the wheel's 0.25) keep that above a line of
        // 10px text on screen. Names that scroll off are simply culled.
        for (i = 0; i < bands.order.length; i++) {
          var bkey = bands.order[i];
          var ly = bands.y[bkey] * zoom + oy;
          if (ly < 6 || ly > rect.height - 6) continue;
          ctx.fillText(bands.label[bkey], 8, ly);
        }
        ctx.restore();
      }
    }

    /* Fit the whole graph to the canvas. The layout above is sized for
     * READABILITY, not for the surface it lands on — so without this a
     * comfortably-spaced neighbourhood would sit half outside a narrow sidebar,
     * and a folder of hundreds would run off the window. It is also what makes
     * the same spacing serve both surfaces: what differs between a 320px panel
     * and a full window is the zoom, not the layout.
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
        minX = Math.min(minX, nodes[i].tx);
        maxX = Math.max(maxX, nodes[i].tx);
        minY = Math.min(minY, nodes[i].ty);
        maxY = Math.max(maxY, nodes[i].ty);
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
        step();
        frameAll();
        if (settling || drag) frame = requestAnimationFrame(tick);
        draw();
      });
    }

    /* Re-run the layout for the current node set — the surface changed shape,
     * so the lane wrapping may want a different answer. Targets move; the
     * nodes glide. */
    function relayout() {
      if (!nodes.length) return;
      var rect = canvas.getBoundingClientRect();
      bands = layout(nodes, edges, rect);
      settling = true;
      run();
    }

    function setData(payload) {
      var rect = canvas.getBoundingClientRect();
      var byId = {};
      /* Positions are CARRIED OVER for ids we already have. Load-bearing:
       * every autosave re-sends the graph (MD-9), so without this the picture
       * re-assembled itself every 2 seconds while you typed — the same "yank
       * the view out from under someone mid-read" frameAll refuses to do,
       * arriving by another door. A carried-over node keeps its position and
       * glides if its assignment moved; a NEW node is born at its assignment,
       * because watching the newcomer arrive from nowhere says nothing. */
      var previous = {};
      for (var p = 0; p < nodes.length; p++) previous[nodes[p].id] = nodes[p];
      widest = 0;
      var list = (payload.nodes || []).map(function (node) {
        var kept = previous[node.id];
        var made = {
          id: node.id, kind: node.kind, label: node.label,
          // `label` is a DISPLAY string (a note's is its filename); `target` is
          // the authored link target a ghost was made from. Creating a note is
          // a path operation, so it reads target and never label.
          path: node.path, target: node.target, degree: node.degree,
          dir: node.dir, focus: node.id === payload.focus,
          x: kept ? kept.x : NaN, y: kept ? kept.y : NaN,
          tx: 0, ty: 0,
          // A pin the user set by dragging survives a re-send: their placement
          // is theirs to keep, on both axes.
          userPinned: kept ? kept.userPinned : false,
        };
        byId[node.id] = made;
        return made;
      });
      var linked = (payload.edges || []).map(function (edge) {
        return { kind: edge.kind, a: byId[edge.source], b: byId[edge.target] };
      }).filter(function (edge) { return edge.a && edge.b; });

      measureLabels(list);
      nodes = list;
      edges = linked;
      bands = layout(nodes, edges, rect);
      for (var i = 0; i < nodes.length; i++) {
        if (isNaN(nodes[i].x)) { nodes[i].x = nodes[i].tx; nodes[i].y = nodes[i].ty; }
      }
      settling = true;
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
        // Drag-to-pin: past this point the layout no longer places this node,
        // so a node lifted out of its row to be read stays lifted.
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

    function onResize() { relayout(); }

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
