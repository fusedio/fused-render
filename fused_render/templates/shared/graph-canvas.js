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
 * Behaviours copied from Obsidian's graph: node radius scales with degree,
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

    function radius(node) { return 3.5 + Math.sqrt(node.degree) * 2.6; }

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
      var cy = rect.height / 2;
      var i, j;
      for (i = 0; i < nodes.length; i++) {
        var a = nodes[i];
        for (j = 0; j < nodes.length; j++) {
          if (i === j) continue;
          var b = nodes[j];
          var dx = a.x - b.x;
          var dy = a.y - b.y;
          var d2 = dx * dx + dy * dy;
          if (d2 < 0.01) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 0.01; }
          var d = Math.sqrt(d2);
          var push = 1400 / d2;
          a.vx += (dx / d) * push;
          a.vy += (dy / d) * push;
        }
        a.vx += (cx - a.x) * 0.005;
        a.vy += (cy - a.y) * 0.005;
      }
      for (i = 0; i < edges.length; i++) {
        var edge = edges[i];
        var ex = edge.b.x - edge.a.x;
        var ey = edge.b.y - edge.a.y;
        var ed = Math.max(1, Math.sqrt(ex * ex + ey * ey));
        var pull = (ed - 70) * 0.02;
        edge.a.vx += (ex / ed) * pull;
        edge.a.vy += (ey / ed) * pull;
        edge.b.vx -= (ex / ed) * pull;
        edge.b.vy -= (ey / ed) * pull;
      }
      for (i = 0; i < nodes.length; i++) {
        var n = nodes[i];
        if (n.pinned || n === drag) { n.vx = 0; n.vy = 0; continue; }
        n.x += n.vx * alpha;
        n.y += n.vy * alpha;
        n.vx *= 0.82;
        n.vy *= 0.82;
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
      ctx.translate(ox, oy);
      ctx.scale(zoom, zoom);

      var accent = token("--accent");
      var muted = token("--fg-muted");
      var ghost = token("--ghost");
      var line = token("--border");
      var near = hover ? neighbours(hover) : null;
      var i;

      ctx.lineWidth = 1 / zoom;
      for (i = 0; i < edges.length; i++) {
        var edge = edges[i];
        var lit = near && near[edge.a.id] && near[edge.b.id];
        ctx.strokeStyle = lit ? accent : line;
        ctx.setLineDash(
          edge.a.kind === "ghost" || edge.b.kind === "ghost" ? [3, 3] : []);
        ctx.beginPath();
        ctx.moveTo(edge.a.x, edge.a.y);
        ctx.lineTo(edge.b.x, edge.b.y);
        ctx.stroke();
      }
      ctx.setLineDash([]);

      var labels = zoom > 0.75;
      ctx.font = "11px -apple-system, BlinkMacSystemFont, sans-serif";
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
    }

    function run() {
      if (frame || dead) return;
      frame = requestAnimationFrame(function tick() {
        frame = null;
        if (dead) return;
        if (alpha > 0.02 || drag) {
          step();
          frame = requestAnimationFrame(tick);
        }
        draw();
      });
    }

    function setData(payload) {
      var rect = canvas.getBoundingClientRect();
      var cx = rect.width / 2 || 160;
      var cy = rect.height / 2 || 160;
      var byId = {};
      nodes = (payload.nodes || []).map(function (node, i) {
        var angle = (i / Math.max(1, payload.nodes.length)) * Math.PI * 2;
        var focused = node.id === payload.focus;
        var seeded = {
          id: node.id, kind: node.kind, label: node.label,
          path: node.path, degree: node.degree,
          x: focused ? cx : cx + Math.cos(angle) * 70,
          y: focused ? cy : cy + Math.sin(angle) * 70,
          vx: 0, vy: 0,
          // The focus is pinned at the centre: a local graph is *about* one
          // note, so letting the sim carry it away loses the point.
          pinned: focused, focus: focused,
        };
        byId[node.id] = seeded;
        return seeded;
      });
      edges = (payload.edges || []).map(function (edge) {
        return { kind: edge.kind, a: byId[edge.source], b: byId[edge.target] };
      }).filter(function (edge) { return edge.a && edge.b; });
      alpha = 1;
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
      if (found.node) {
        drag = found.node;
        drag.pinned = true; // drag-to-pin
      } else {
        pan = { x: event.clientX, y: event.clientY };
      }
    }

    function onUp() { drag = null; pan = null; }

    function onClick(event) {
      var found = at(event);
      if (!found.node) return;
      if (found.node.kind === "note" && found.node.path) onOpenNote(found.node.path);
      else if (found.node.kind === "ghost") onCreateGhost(found.node.label);
    }

    function onWheel(event) {
      event.preventDefault();
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
