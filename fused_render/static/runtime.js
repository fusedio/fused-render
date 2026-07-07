/*
 * Injected into every rendered HTML file (see server.py `/render`).
 * Provides `window.fused`:
 *   fused.runPython(pyPath, params) -> Promise<result>
 *   fused.params.get(key) / getAll() / set(key, value) / onChange(cb) -> unsubscribe
 *
 * Same-origin iframe model: this script talks to an ancestor window's URL
 * directly (no postMessage bridge — see DECISIONS.md D3/D4). The param target
 * is the TOPMOST same-origin ancestor (D46), stopping BELOW any ancestor
 * marked as a param boundary (`_fusedParamBoundary` — only tab mode sets one,
 * TM-3/D47): it climbs window.parent while the next ancestor is same-origin,
 * reachable, and not a boundary. In normal view/embed mode the direct parent is
 * already the top, so this is unchanged; inside layout mode every pane's
 * rendered page climbs past its embed shell to the layout shell, so params
 * read/write the shared layout URL (merging + cross-pane sync are then
 * structural). Inside tab mode the tab shell is a boundary, so the climb stops
 * at each tab's own embed shell — tab params stay tab-independent (a nested
 * panel still pools among its own panes, its climb halting at the panel shell
 * just below the boundary). When loaded as a top-level
 * page (target === window, e.g. visiting /render?path=... directly, or a
 * cross-origin ancestor) it falls back to reading/writing its own URL, treating
 * the `path` query key as reserved alongside any `_`-prefixed key.
 */
(function () {
  "use strict";

  // Climb to the topmost same-origin ancestor (D46). Reading .location.href on
  // a cross-origin window throws, so a try/catch marks the boundary.
  function findTarget() {
    let t = window;
    try {
      while (t.parent && t.parent !== t) {
        // Probe: throws if t.parent is cross-origin — stop at the last
        // same-origin ancestor. Probe first so cross-origin catch semantics are
        // unchanged, then honor a param boundary (tab shell, TM-3): stop below
        // it so the page targets its own pane's URL, not the shared tab URL.
        void t.parent.location.href;
        if (t.parent._fusedParamBoundary) break;
        t = t.parent;
      }
    } catch (e) {
      /* hit a cross-origin ancestor; t is the topmost same-origin one */
    }
    return t;
  }

  const target = findTarget();
  const standalone = target === window;

  function isReserved(key) {
    if (key.startsWith("_")) return true;
    if (standalone && key === "path") return true;
    return false;
  }

  // In layout mode the target URL carries the reserved `_layout` param, which
  // is parenthesized and may contain LITERAL `&` (D51 — see the shell's
  // layout-codec.js, whose balanced-paren scan this duplicates: the runtime is
  // injected standalone and imports nothing). Raw URLSearchParams would split
  // inside the parens and leak layout fragments as visible params, so every
  // read/write splits the search string first: `layoutSpan` is the raw
  // `_layout=(...)` span (preserved byte-for-byte, reinserted last on write —
  // never decoded here; only the layout shells parse it), `rest` is the
  // remainder. Literal parens in the span are structural and balanced by
  // construction (codec-escaped otherwise); an unbalanced span (truncated URL)
  // runs to end-of-string so it still can't pollute `rest`.
  function splitSearch(search) {
    const s = (search || "").replace(/^\?/, "");
    const m = /(^|&)_layout=\(/.exec(s);
    if (!m) return { layoutSpan: null, rest: s };
    const start = m.index + m[1].length;
    let i = start + "_layout=(".length;
    let depth = 1;
    while (i < s.length && depth > 0) {
      if (s[i] === "(") depth++;
      else if (s[i] === ")") depth--;
      i++;
    }
    return {
      layoutSpan: s.slice(start, i),
      rest: (s.slice(0, m.index) + s.slice(i)).replace(/^&|&$/g, ""),
    };
  }

  function currentParams() {
    return new URLSearchParams(splitSearch(target.location.search).rest);
  }

  const listeners = new Set();

  // Only the visible (non-reserved) params matter to onChange; snapshotting
  // that lets notifyIfChanged() skip no-op fires and notification loops (D46).
  let lastSnapshot = null;

  function fire(snapshot) {
    for (const cb of listeners) {
      try {
        cb(snapshot);
      } catch (e) {
        console.error("[fused] params.onChange listener threw:", e);
      }
    }
  }

  // Fire onChange only when the visible param snapshot actually changed. This
  // is the single notification channel — set() and any ancestor URL change
  // both route through the fused:urlchange event, and the diff guard kills the
  // duplicate a self-set would otherwise produce.
  function notifyIfChanged() {
    const snapshot = getAll();
    const serialized = JSON.stringify(snapshot);
    if (serialized === lastSnapshot) return;
    lastSnapshot = serialized;
    fire(snapshot);
  }

  function get(key) {
    if (key === "_file") {
      // _file normally rides on this frame's own URL (set by the shell on the
      // iframe src). Fall back to the shell URL for manually-opened views like
      // /view/<template>.html?_file=<target>.
      const own = new URLSearchParams(window.location.search);
      if (own.has("_file")) return own.get("_file");
      const outer = currentParams();
      return outer.has("_file") ? outer.get("_file") : undefined;
    }
    const params = currentParams();
    if (isReserved(key)) return undefined;
    return params.has(key) ? params.get(key) : undefined;
  }

  function getAll() {
    const params = currentParams();
    const result = {};
    for (const [key, value] of params) {
      if (isReserved(key)) continue;
      result[key] = value;
    }
    const file = get("_file");
    if (file !== undefined) result._file = file;
    return result;
  }

  function set(key, value) {
    if (isReserved(key)) {
      throw new Error(`fused.params.set: '${key}' is a reserved param name and cannot be set`);
    }
    if (typeof value !== "string") {
      throw new Error(
        `fused.params.set: value for '${key}' must be a string, got ${typeof value}`
      );
    }
    const { layoutSpan, rest } = splitSearch(target.location.search);
    const params = new URLSearchParams(rest);
    params.set(key, value);
    // Rebuild with the raw `_layout=(...)` span untouched and LAST (D51): the
    // layout stays readable (no URLSearchParams.toString() percent-soup) and
    // the global/local boundary stays visually stable.
    let search = params.toString();
    if (layoutSpan) search += (search ? "&" : "") + layoutSpan;
    const newUrl = target.location.pathname + (search ? "?" + search : "");
    target.history.replaceState(target.history.state, "", newUrl);
    // Notify via the event path only (no direct notify(), D46). When the shell
    // wrapper exists it also fires fused:urlchange on replaceState — the
    // snapshot diff in notifyIfChanged() makes the duplicate harmless; this
    // explicit dispatch covers standalone /render pages that have no wrapper.
    target.dispatchEvent(new Event("fused:urlchange"));
  }

  function onChange(cb) {
    listeners.add(cb);
    return () => listeners.delete(cb);
  }

  // Baseline the snapshot at load so the first no-op fused:urlchange doesn't
  // fire, while a real set() (which changes a param) still does.
  lastSnapshot = JSON.stringify(getAll());

  // Single notification channel: any change to the target window's URL — our
  // own set(), a sibling pane's set() (same target in layout mode), or the
  // shell's own history writes — arrives as fused:urlchange (D46/LM-8).
  target.addEventListener("fused:urlchange", notifyIfChanged);

  function runPython(pyPath, params) {
    const ownPath = new URLSearchParams(window.location.search).get("path");
    return fetch("/api/run", {
      method: "POST",
      // X-Fused forces a CORS preflight so a foreign page can't fire this
      // execute endpoint blind (see server.py _require_fused).
      headers: { "Content-Type": "application/json", "X-Fused": "1" },
      body: JSON.stringify({ py: pyPath, html: ownPath, params: params || {} }),
    })
      .then((res) => res.json())
      .then((data) => {
        if (data.stdout) {
          console.log("[python]", data.stdout);
        }
        // Watch the executed file for auto-reload, even on failure (LR-2): a
        // broken py that gets fixed must still trigger a reload. Read before
        // the ok check so it's recorded either way.
        if (data.resolved_py) watchPath(data.resolved_py);
        if (!data.ok) {
          const err = new Error(data.error && data.error.message);
          err.type = data.error && data.error.type;
          err.traceback = data.error && data.error.traceback;
          err.where = data.error && data.error.where; // {file, line, func, source} in the USER's script, or null
          err.stdout = data.stdout;
          throw err;
        }
        return data.result;
      });
  }

  // Synchronous URL of the raw-bytes endpoint for a file — for <img>/<embed>
  // src, "open raw" links, etc.
  function rawUrl(path) {
    return "/api/fs/raw?path=" + encodeURIComponent(path);
  }

  // Fetch file metadata (same shape as /api/fs/stat). Rejects with an Error
  // carrying the server's message, mirroring runPython's rejection style.
  function stat(path) {
    return fetch("/api/fs/stat?path=" + encodeURIComponent(path))
      .then((res) => res.json().then((data) => ({ res, data })))
      .then(({ res, data }) => {
        if (!res.ok) throw new Error((data && data.error) || "HTTP " + res.status);
        return data;
      });
  }

  // Read a file's text via the raw endpoint.
  function readFile(path) {
    return fetch(rawUrl(path)).then((res) => {
      if (!res.ok) throw new Error("failed to read " + path + " (HTTP " + res.status + ")");
      return res.text();
    });
  }

  // Write UTF-8 text to a file, returning the fresh stat object. opts:
  //   { expectedMtime } — optimistic lock; omit to write unconditionally.
  // A 409 becomes an Error with `type: "conflict"` and the server's current
  // `mtime` attached, so callers can offer reload/overwrite.
  function writeFile(path, content, opts) {
    const payload = { path: path, content: content };
    if (opts && opts.expectedMtime !== undefined && opts.expectedMtime !== null) {
      payload.expected_mtime = opts.expectedMtime;
    }
    return fetch("/api/fs/write", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Fused": "1" },
      body: JSON.stringify(payload),
    })
      .then((res) => res.json().then((data) => ({ res, data })))
      .then(({ res, data }) => {
        if (res.status === 409) {
          const err = new Error("file changed on disk");
          err.type = "conflict";
          err.mtime = data && data.mtime;
          throw err;
        }
        if (!res.ok) throw new Error((data && data.error) || "HTTP " + res.status);
        return data;
      });
  }

  // --- Auto-reload (SPEC §13.3) ---------------------------------------------
  // This page watches a set of files via the SSE change feed; on any change it
  // reloads THIS frame (honest re-execution — we can't replay what the page did
  // with a python result). All reload logic lives here so every rendered page
  // (view, embed, standalone /render) gets it for free.
  let autoReloadEnabled = true;
  const watched = new Set();
  let es = null;
  let started = false;       // watching begins on DOMContentLoaded (LR-5)
  let resubscribeTimer = null;
  let reloadTimer = null;

  function resubscribe() {
    resubscribeTimer = null;
    if (es) {
      es.close();
      es = null;
    }
    if (!autoReloadEnabled || watched.size === 0) return;
    const query = [...watched].map((p) => "path=" + encodeURIComponent(p)).join("&");
    es = new EventSource("/api/fs/events?" + query);
    es.onmessage = () => {
      // Any change (including deletion, mtime: null → LR-6) reloads after a
      // 300 ms debounce that coalesces bursts.
      clearTimeout(reloadTimer);
      reloadTimer = setTimeout(() => window.location.reload(), 300);
    };
    // EventSource reconnects on error by default — leave it.
  }

  function watchPath(p) {
    if (!p || watched.has(p)) return;
    watched.add(p);
    if (!autoReloadEnabled || !started) return; // before start, paths just accumulate
    // Debounce resubscribe so a page firing several runPython calls on load
    // reconnects once (LR-4).
    clearTimeout(resubscribeTimer);
    resubscribeTimer = setTimeout(resubscribe, 100);
  }

  function autoReload(enabled) {
    autoReloadEnabled = !!enabled;
    if (!autoReloadEnabled) {
      clearTimeout(resubscribeTimer);
      clearTimeout(reloadTimer);
      if (es) {
        es.close();
        es = null;
      }
    } else if (started) {
      resubscribe();
    }
  }

  function startAutoReload() {
    started = true;
    // Union of: this page's own rendered file, _file if present (LR-1).
    const params = new URLSearchParams(window.location.search);
    const own = params.get("path");
    if (own) watched.add(own);
    const file = params.get("_file");
    if (file) watched.add(file);
    if (autoReloadEnabled) resubscribe();
  }

  window.fused = {
    runPython,
    rawUrl,
    stat,
    readFile,
    writeFile,
    autoReload,
    params: { get, getAll, set, onChange },
  };

  // Error overlay: shows for unhandled runPython rejections the page didn't
  // catch itself (identified by carrying a `.traceback`).
  function showOverlay(err) {
    const overlay = document.createElement("div");
    overlay.style.cssText = [
      "position:fixed", "inset:0", "z-index:2147483647",
      "background:rgba(20,0,0,0.92)", "color:#ffdede",
      "font-family:ui-monospace,Menlo,Consolas,monospace",
      "font-size:13px", "padding:24px", "overflow:auto",
      "border:4px solid #c0392b", "box-sizing:border-box",
      "white-space:pre-wrap",
    ].join(";");
    const title = document.createElement("div");
    title.style.cssText = "font-size:16px;font-weight:bold;margin-bottom:12px;color:#ff6b6b;";
    title.textContent = `${err.type || "Error"}: ${err.message || ""}`;
    overlay.appendChild(title);
    // Headline the failing line of the USER's script (err.where, set by the
    // executor) so the culprit is readable without scanning the traceback.
    if (err.where && err.where.file) {
      const loc = document.createElement("div");
      loc.style.cssText = "margin-bottom:12px;color:#ffb3b3;";
      const func = err.where.func ? `, in ${err.where.func}` : "";
      loc.textContent = `${err.where.file}, line ${err.where.line}${func}`;
      if (err.where.source) {
        const src = document.createElement("div");
        src.style.cssText = "opacity:0.8;padding-left:2ch;";
        src.textContent = err.where.source;
        loc.appendChild(src);
      }
      overlay.appendChild(loc);
    }
    const pre = document.createElement("pre");
    pre.style.cssText = "margin:0;white-space:pre-wrap;word-break:break-word;";
    pre.textContent = err.traceback || "";
    overlay.appendChild(pre);
    document.body.appendChild(overlay);
  }

  window.addEventListener("unhandledrejection", (event) => {
    const err = event.reason;
    if (err && err.traceback) {
      showOverlay(err);
    }
  });

  // Start watching after inline page scripts have run, so an opt-out via
  // fused.autoReload(false) (e.g. the code editor) wins the race (LR-5).
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startAutoReload);
  } else {
    startAutoReload();
  }
})();
