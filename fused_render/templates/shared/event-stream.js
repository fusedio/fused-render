/* Event-stream studio chrome — the reusable pieces behind an "arrivals over
 * time + filterable, paged, expandable rows" view (webhook_debugger).
 * Framework-free, no deps, served from /template-shared/ like ro-badge.js.
 *
 * Load:  <script src="/template-shared/event-stream.js"></script>
 * Use:   window.EventStream.{escapeHtml, matcher, readParams, setParams,
 *                            Histogram, expandable}
 */
(function () {
  "use strict";

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function number(value, fallback) {
    var parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : (fallback === undefined ? 0 : fallback);
  }

  // Same grammar as log_studio/reader.py _query_matcher: a plain substring
  // (case-insensitive), an `re:...` prefix, or a `/regex/` wrap. Throws on a
  // bad pattern so callers can surface it; substring never throws.
  function matcher(q) {
    q = String(q == null ? "" : q).trim();
    if (q.indexOf("re:") === 0) {
      var re = new RegExp(q.slice(3));
      return function (text) { return re.test(text); };
    }
    if (q.length > 2 && q[0] === "/" && q[q.length - 1] === "/") {
      var rx = new RegExp(q.slice(1, -1));
      return function (text) { return rx.test(text); };
    }
    var needle = q.toLowerCase();
    return function (text) {
      return !needle || String(text).toLowerCase().indexOf(needle) !== -1;
    };
  }

  // Mirrors log_studio's currentState/setParams over fused.params. `defs` maps
  // a param key to its default (string) — refreshing the page reproduces the
  // view because every piece of state lives in the URL.
  function readParams(fused, defs) {
    var out = {};
    for (var key in defs) {
      if (Object.prototype.hasOwnProperty.call(defs, key)) {
        var value = fused.params.get(key);
        out[key] = value == null || value === "" ? defs[key] : value;
      }
    }
    return out;
  }

  function setParams(fused, values) {
    for (var key in values) {
      if (Object.prototype.hasOwnProperty.call(values, key)) {
        var next = String(values[key]);
        if ((fused.params.get(key) || "") !== next) fused.params.set(key, next);
      }
    }
  }

  // Click-to-expand delegation for a paged row list: clicking a row toggles the
  // `expanded` class and flips the +/- marker. `opts.ignore` is a selector for
  // interactive controls inside a row that must not toggle it.
  function expandable(container, opts) {
    opts = opts || {};
    var rowSelector = opts.row || ".es-row";
    var markSelector = opts.mark || ".es-expand-mark";
    container.addEventListener("click", function (event) {
      if (opts.ignore && event.target.closest(opts.ignore)) return;
      var row = event.target.closest(rowSelector);
      if (!row || !container.contains(row)) return;
      var open = row.classList.toggle("expanded");
      var mark = row.querySelector(markSelector);
      if (mark) mark.textContent = open ? "−" : "+";
      if (opts.onToggle) opts.onToggle(row, open);
    });
  }

  // Collapsible facet rail: `btn` toggles `className` on `app`, and (when
  // opts.autoCollapse is a pixel width) the rail auto-collapses while `app` is
  // narrower than that — so the view reflows in a small or embedded preview
  // pane. A manual toggle wins until the width next crosses the threshold, then
  // auto-collapsing resumes.
  function railToggle(app, btn, opts) {
    opts = opts || {};
    var cls = opts.className || "facets-collapsed";
    var below = opts.autoCollapse || 0;
    var override = null;
    var wasNarrow = null;

    function apply() {
      var narrow = below ? app.clientWidth < below : false;
      var collapsed = override !== null ? override : narrow;
      app.classList.toggle(cls, !!collapsed);
      if (btn) {
        btn.setAttribute("aria-pressed", String(!!collapsed));
        btn.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
      }
    }
    if (btn) {
      btn.addEventListener("click", function () {
        override = !app.classList.contains(cls);
        apply();
      });
    }
    if (below && typeof ResizeObserver !== "undefined") {
      new ResizeObserver(function () {
        var narrow = app.clientWidth < below;
        if (wasNarrow !== null && narrow !== wasNarrow) override = null;
        wasNarrow = narrow;
        apply();
      }).observe(app);
    }
    apply();
    return { apply: apply };
  }

  // Time-bucketed arrivals histogram + pointer-drag time brush on a <canvas>.
  // The bars are stacked by category (HTTP method, log level, …); colors come
  // from opts.colorFor(category). A completed drag calls opts.onBrush(from, to)
  // with epoch-second bounds; opts.formatTime(epoch) labels the axis + tooltip.
  function Histogram(canvas, opts) {
    opts = opts || {};
    var ctx = canvas.getContext("2d");
    var wrap = canvas.parentElement;
    var colorFor = opts.colorFor || function () { return "#888"; };
    var formatTime = opts.formatTime || function (v) { return String(v); };
    var cssVar = function (name, fallback) {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      return v || fallback;
    };
    var data = null;      // { bins: [{start,end,counts:{cat:n}}], min, max }
    var selection = null; // { from, to }
    var brush = null;
    var tooltip = opts.tooltip || null;

    function bounds() {
      var width = canvas.clientWidth;
      var height = canvas.clientHeight;
      return { left: 40, right: Math.max(41, width - 8), top: 6,
               bottom: Math.max(7, height - 20), width: width, height: height };
    }

    function xForTime(value, b, min, max) {
      return b.left + ((value - min) / Math.max(1e-9, max - min)) * (b.right - b.left);
    }

    function timeForX(value, b, min, max) {
      var ratio = Math.max(0, Math.min(1, (value - b.left) / Math.max(1, b.right - b.left)));
      return min + ratio * (max - min);
    }

    function categoriesIn(bins) {
      var found = {};
      bins.forEach(function (bin) {
        for (var cat in bin.counts) if (bin.counts[cat]) found[cat] = true;
      });
      var list = Object.keys(found);
      if (opts.order) {
        list.sort(function (a, b) {
          var ai = opts.order.indexOf(a); var bi = opts.order.indexOf(b);
          return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
        });
      }
      return list;
    }

    function draw() {
      var b = bounds();
      ctx.clearRect(0, 0, b.width, b.height);
      ctx.fillStyle = cssVar("--rail", "#12161c");
      ctx.fillRect(0, 0, b.width, b.height);
      var bins = data ? data.bins : [];
      if (!bins.length) {
        ctx.fillStyle = cssVar("--axis", "#687281");
        ctx.font = "11px ui-sans-serif, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(opts.emptyLabel || "No events in this selection",
                     b.width / 2, b.height / 2);
        return;
      }
      var min = data.min;
      var max = data.max > min ? data.max : min + 1;
      var cats = categoriesIn(bins).reverse();
      var totals = bins.map(function (bin) {
        var sum = 0; for (var c in bin.counts) sum += number(bin.counts[c]); return sum;
      });
      var peak = Math.max.apply(null, [1].concat(totals));

      ctx.strokeStyle = cssVar("--grid", "#252c35");
      ctx.lineWidth = 1;
      ctx.fillStyle = cssVar("--axis", "#687281");
      ctx.font = "10px ui-monospace, monospace";
      ctx.textAlign = "right";
      for (var i = 0; i <= 2; i += 1) {
        var y = b.bottom - ((b.bottom - b.top) * i) / 2;
        ctx.beginPath();
        ctx.moveTo(b.left, y + 0.5);
        ctx.lineTo(b.right, y + 0.5);
        ctx.stroke();
        ctx.fillText(String(Math.round((peak * i) / 2)), b.left - 5, y + 3);
      }

      var slot = (b.right - b.left) / bins.length;
      var barWidth = Math.max(1, slot - Math.min(2, slot * 0.16));
      bins.forEach(function (bin, index) {
        var y = b.bottom;
        cats.forEach(function (cat) {
          var count = number(bin.counts[cat]);
          if (!count) return;
          var h = Math.max(1, (count / peak) * (b.bottom - b.top));
          y -= h;
          ctx.fillStyle = colorFor(cat);
          ctx.fillRect(b.left + index * slot + (slot - barWidth) / 2, y, barWidth, h);
        });
      });

      function overlay(x1, x2, fill) {
        ctx.fillStyle = fill;
        ctx.fillRect(Math.min(x1, x2), b.top, Math.abs(x2 - x1), b.bottom - b.top);
        ctx.strokeStyle = cssVar("--accent", "#55b8d2");
        ctx.strokeRect(Math.min(x1, x2) + 0.5, b.top + 0.5,
                       Math.abs(x2 - x1), b.bottom - b.top - 1);
      }
      if (selection && Number.isFinite(selection.from) && Number.isFinite(selection.to)) {
        overlay(xForTime(selection.from, b, min, max),
                xForTime(selection.to, b, min, max), "rgba(85,184,210,0.10)");
      }
      if (brush) {
        overlay(Math.max(b.left, Math.min(b.right, brush.start)),
                Math.max(b.left, Math.min(b.right, brush.current)),
                "rgba(85,184,210,0.18)");
      }

      ctx.fillStyle = cssVar("--axis", "#687281");
      ctx.textAlign = "left";
      ctx.fillText(formatTime(min), b.left, b.height - 5);
      ctx.textAlign = "right";
      ctx.fillText(formatTime(max), b.right, b.height - 5);
    }

    function resize() {
      var rect = canvas.getBoundingClientRect();
      var dpr = Math.min(window.devicePixelRatio || 1, 2.5);
      var width = Math.max(1, Math.round(rect.width * dpr));
      var height = Math.max(1, Math.round(rect.height * dpr));
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw();
    }

    function canvasX(event) {
      return event.clientX - canvas.getBoundingClientRect().left;
    }

    canvas.addEventListener("pointerdown", function (event) {
      if (!data || !data.bins.length) return;
      brush = { start: canvasX(event), current: canvasX(event), pointerId: event.pointerId };
      canvas.setPointerCapture(event.pointerId);
      if (tooltip) tooltip.style.display = "none";
      draw();
    });

    canvas.addEventListener("pointermove", function (event) {
      if (!data || !data.bins.length) return;
      var x = canvasX(event);
      if (brush && brush.pointerId === event.pointerId) {
        brush.current = x;
        draw();
        return;
      }
      if (!tooltip) return;
      var b = bounds();
      if (x < b.left || x > b.right) { tooltip.style.display = "none"; return; }
      var index = Math.min(data.bins.length - 1,
        Math.floor(((x - b.left) / (b.right - b.left)) * data.bins.length));
      var bin = data.bins[index];
      var entries = Object.keys(bin.counts).filter(function (c) { return number(bin.counts[c]) > 0; });
      tooltip.innerHTML = "<strong>" + escapeHtml(formatTime(bin.start)) + "</strong><br>" +
        entries.map(function (c) { return escapeHtml(c) + " " + number(bin.counts[c]); }).join(" / ");
      tooltip.style.display = "block";
      tooltip.style.left = Math.min(wrap.clientWidth - tooltip.offsetWidth - 6,
        Math.max(6, x + 10)) + "px";
      tooltip.style.top = Math.max(4, event.clientY - wrap.getBoundingClientRect().top -
        tooltip.offsetHeight - 8) + "px";
    });

    canvas.addEventListener("pointerleave", function () {
      if (!brush && tooltip) tooltip.style.display = "none";
    });

    canvas.addEventListener("pointerup", function (event) {
      if (!brush || brush.pointerId !== event.pointerId || !data) return;
      var done = brush;
      brush = null;
      draw();
      if (Math.abs(done.current - done.start) < 5) return;
      var b = bounds();
      var min = data.min;
      var max = data.max > min ? data.max : min + 1;
      var from = timeForX(Math.min(done.start, done.current), b, min, max);
      var to = timeForX(Math.max(done.start, done.current), b, min, max);
      if (opts.onBrush) opts.onBrush(Math.floor(from), Math.ceil(to));
    });

    canvas.addEventListener("pointercancel", function () { brush = null; draw(); });

    if (typeof ResizeObserver !== "undefined") {
      new ResizeObserver(resize).observe(wrap);
    }

    return {
      setData: function (next) { data = next; resize(); },
      setSelection: function (from, to) {
        selection = (Number.isFinite(from) && Number.isFinite(to)) ? { from: from, to: to } : null;
        draw();
      },
      resize: resize,
      draw: draw
    };
  }

  window.EventStream = {
    escapeHtml: escapeHtml,
    matcher: matcher,
    readParams: readParams,
    setParams: setParams,
    expandable: expandable,
    railToggle: railToggle,
    Histogram: Histogram
  };
})();
