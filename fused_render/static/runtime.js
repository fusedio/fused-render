/*
 * Injected into every rendered HTML file (see server.py `/render`).
 * Provides `window.fused`:
 *   fused.runPython(pyPath, params, opts?) -> Promise<result>
 *     opts.key    — a latest-wins request channel: a newer call on the same key
 *                   aborts the prior in-flight one (D113 — cancel stale scrubs).
 *     opts.signal — a caller AbortSignal; composes with the channel abort.
 *   fused.params.get(key) / getAll() / set(key, value) / onChange(cb) -> unsubscribe
 *
 * Same-origin iframe model: this script talks to an ancestor window's URL
 * directly (no postMessage bridge — see DECISIONS.md D3/D4). The param target
 * is the TOPMOST same-origin ancestor (D46), stopping BELOW any ancestor
 * marked as a param boundary (`_fusedParamBoundary` — both layout shells set
 * one, LM-3/TM-3/D72): it climbs window.parent while the next ancestor is
 * same-origin, reachable, and not a boundary. In normal view/embed mode the
 * direct parent is already the top, so this is unchanged; inside a layout mode
 * (panel or tab) the shell is a boundary, so the climb stops at each pane's/
 * tab's own embed shell — params stay pane-local, captured segment-local
 * inside `_layout` by the shell's ordinary URL sync.
 *
 * Global params still exist but only by hand (D72): top-level params a user
 * types on a layout shell URL are READABLE from every pane (get/getAll merge
 * the same-origin ancestor chain above the boundary; nearer wins, pane-local
 * wins over all), but set() never writes them — writes always land on the
 * pane's own URL. When loaded as a top-level
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

  // Same-origin ancestors ABOVE the target, nearest first (non-empty only when
  // a param boundary stopped the climb). Their top-level queries hold
  // hand-typed global params (D72): read-only from here — set() never touches
  // them. Computed per read: an ancestor's URL changes over time.
  function ancestorWindows() {
    const out = [];
    let t = target;
    try {
      while (t.parent && t.parent !== t) {
        void t.parent.location.href; // throws when cross-origin
        t = t.parent;
        out.push(t);
      }
    } catch (e) {
      /* hit a cross-origin ancestor — chain ends */
    }
    return out;
  }

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

  // ---- coalesced history writes (D99) ---------------------------------------
  // WebKit (Safari, and the WKWebView the menu-bar popover uses, §25) hard-
  // limits history.replaceState/pushState to 100 calls per 30 s — past that it
  // THROWS SecurityError, which would kill the caller mid-scrub. Chrome has no
  // such limit, so this only ever bit inside the popover. Params therefore
  // take effect immediately through a pending-search overlay (targetSearch()),
  // while the actual history write is rate-limited to one per
  // HISTORY_MIN_INTERVAL_MS with a trailing flush — a scrub burst costs ~75
  // writes/30 s, safely under the cap, and the URL still lands on the final
  // value.
  const HISTORY_MIN_INTERVAL_MS = 400;
  let pendingSearch = null; // what target.location.search WILL be after flush
  let pendingUrl = null;
  let historyTimer = null;
  let lastHistoryWrite = 0;

  function targetSearch() {
    return pendingSearch !== null ? pendingSearch : target.location.search;
  }

  function flushHistory() {
    historyTimer = null;
    if (pendingUrl === null) return;
    const url = pendingUrl;
    pendingUrl = null;
    pendingSearch = null;
    lastHistoryWrite = Date.now();
    try {
      target.history.replaceState(target.history.state, "", url);
    } catch (e) {
      // WebKit throttle hit anyway (e.g. another writer burned the budget).
      // The overlay already served readers; losing one URL write is benign.
      console.warn("[fused] history write throttled:", e);
    }
  }

  function currentParams() {
    return new URLSearchParams(splitSearch(targetSearch()).rest);
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
    if (isReserved(key)) return undefined;
    const params = currentParams();
    if (params.has(key)) return params.get(key);
    // Hand-typed global fallback (D72): nearest ancestor above the boundary
    // that carries the key wins.
    for (const win of ancestorWindows()) {
      const p = new URLSearchParams(splitSearch(win.location.search).rest);
      if (p.has(key)) return p.get(key);
    }
    return undefined;
  }

  function getAll() {
    const result = {};
    // Farthest ancestor first, then nearer, then the target's own params —
    // later writes overwrite, so pane-local wins over hand-typed globals (D72).
    const chain = ancestorWindows().reverse();
    chain.push(target);
    for (const win of chain) {
      const search = win === target ? targetSearch() : win.location.search;
      const params = new URLSearchParams(splitSearch(search).rest);
      for (const [key, value] of params) {
        if (isReserved(key)) continue;
        result[key] = value;
      }
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
    const { layoutSpan, rest } = splitSearch(targetSearch());
    const params = new URLSearchParams(rest);
    params.set(key, value);
    // Rebuild with the raw `_layout=(...)` span untouched and LAST (D51): the
    // layout stays readable (no URLSearchParams.toString() percent-soup) and
    // the global/local boundary stays visually stable.
    let search = params.toString();
    if (layoutSpan) search += (search ? "&" : "") + layoutSpan;
    const newSearch = search ? "?" + search : "";
    const newUrl = target.location.pathname + newSearch;
    // First-change-push: the first param write on a pristine history entry
    // pushes a new entry (preserving the as-loaded state for Back), every
    // later write replaces on top of it — so param churn costs at most one
    // entry per visit. "Pristine" is tracked via a flag on history.state, not
    // a JS variable: the flag travels with the entry, so after Back to the
    // pristine entry the next write correctly pushes again (truncating the
    // old forward branch), and it survives reloads. Existing state (e.g. the
    // tab shell's fusedActiveTab) is merged, not clobbered.
    const prevState = target.history.state;
    const unchanged = newSearch === targetSearch();
    if (unchanged) {
      // Nothing to write; fall through to the notification below.
    } else if (prevState && prevState.fusedParamEntry) {
      // Replace-on-top writes are the scrub-hot path: coalesce them (D99).
      // Readers see the new value immediately via the overlay; the history
      // write happens now if the budget allows, else on the trailing timer.
      pendingSearch = newSearch;
      pendingUrl = newUrl;
      if (!historyTimer) {
        const wait = Math.max(
          0,
          HISTORY_MIN_INTERVAL_MS - (Date.now() - lastHistoryWrite)
        );
        if (wait === 0) flushHistory();
        else historyTimer = setTimeout(flushHistory, wait);
      }
    } else {
      // The once-per-visit push: immediate, so Back gets its entry even if
      // the page dies within the debounce window.
      const nextState = Object.assign({}, prevState, { fusedParamEntry: true });
      pendingSearch = null;
      pendingUrl = null;
      lastHistoryWrite = Date.now();
      try {
        target.history.pushState(nextState, "", newUrl);
      } catch (e) {
        console.warn("[fused] history write throttled:", e);
      }
    }
    // Notify via the event path only (no direct notify(), D46). When the shell
    // wrapper exists it also fires fused:urlchange on the history write — the
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
  // own set() or the shell's own history writes — arrives as fused:urlchange
  // (D46/LM-8). Ancestor shells above a boundary are watched too, so an edit
  // to a hand-typed global (D72) also notifies; the snapshot diff guard makes
  // the layout shell's frequent `_layout` re-syncs no-ops here.
  // Target and ancestor shells outlive this document (they survive pane
  // reloads/navigation), so detach on pagehide — otherwise every reload
  // stacks another stale notifyIfChanged on the shared shell windows.
  const hookedWindows = [target, ...ancestorWindows()];
  for (const win of hookedWindows) {
    win.addEventListener("fused:urlchange", notifyIfChanged);
  }
  window.addEventListener("pagehide", () => {
    // A pending coalesced write (D99) must not die with this document — the
    // URL is the bookmarkable truth.
    flushHistory();
    for (const win of hookedWindows) {
      try {
        win.removeEventListener("fused:urlchange", notifyIfChanged);
      } catch (e) {
        /* window already gone */
      }
    }
  });

  // ---- stale-request cancellation (D113) ------------------------------------
  // Requests sharing an `opts.key` form a latest-wins channel: firing a newer
  // call on a key ABORTS the prior in-flight call on that same key. This is the
  // slider primitive — scrubbing through many values leaves only the last
  // value's request alive; each superseded fetch is cancelled, freeing the
  // browser connection and letting the server abandon the now-irrelevant
  // subprocess once it notices the dropped socket. RH-4 is preserved: with NO
  // key, calls never cancel each other, so unrelated concurrent fetches and
  // same-file polling loops are untouched. An author-supplied `opts.signal`
  // composes — the fetch aborts on whichever fires first. A superseded/aborted
  // call rejects with a standard AbortError (DOMException, name "AbortError");
  // the unhandledrejection handler below treats that as benign, so a
  // fire-and-forget re-render that loses the race neither shows the traceback
  // overlay nor logs an "uncaught (in promise)" to the console.
  const inflightByKey = new Map();

  function runPython(pyPath, params, opts) {
    opts = opts || {};
    const key = opts.key;
    const keyed = key !== undefined && key !== null;
    const controller = new AbortController();
    if (keyed) {
      const prev = inflightByKey.get(key);
      if (prev) prev.abort(); // supersede the now-stale request on this channel
      inflightByKey.set(key, controller);
    }
    if (opts.signal) {
      if (opts.signal.aborted) controller.abort();
      else opts.signal.addEventListener("abort", () => controller.abort(), { once: true });
    }
    // Free the channel slot when this call settles — but only if it is still
    // ours (a newer same-key call may have already replaced us in the map).
    const clearSlot = () => {
      if (keyed && inflightByKey.get(key) === controller) inflightByKey.delete(key);
    };
    const ownPath = new URLSearchParams(window.location.search).get("path");
    return fetch("/api/run", {
      method: "POST",
      // X-Fused forces a CORS preflight so a foreign page can't fire this
      // execute endpoint blind (see server.py _require_fused).
      headers: { "Content-Type": "application/json", "X-Fused": "1" },
      body: JSON.stringify({ py: pyPath, html: ownPath, params: params || {} }),
      signal: controller.signal,
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
          err.stdout = data.stdout;
          throw err;
        }
        return data.result;
      })
      .finally(clearSlot);
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
  // `mtime` attached, so callers can offer reload/overwrite. A read-only
  // refusal (403 {"error":"readonly"}) becomes `type: "readonly"` — the
  // backstop for templates that never checked stat().writable.
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
        if (res.status === 403 && data && data.error === "readonly") {
          const err = new Error("file is read-only");
          err.type = "readonly";
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
    // A reconnect timer may be pending (onclose below); a direct call must
    // cancel it or the stale timer would close and reopen the fresh socket.
    clearTimeout(resubscribeTimer);
    resubscribeTimer = null;
    if (es) {
      const old = es;
      es = null; // null first so old.onclose knows the close was deliberate
      old.close();
    }
    if (!autoReloadEnabled || watched.size === 0) return;
    const query = [...watched].map((p) => "path=" + encodeURIComponent(p)).join("&");
    // WebSocket, not EventSource (D74): SSE holds an HTTP/1.1 socket per open
    // pane and Chrome caps those at 6 per origin — a 6-pane panel starved
    // every later fetch (runPython hung forever). WS has its own, much larger
    // connection pool.
    const proto = window.location.protocol === "https:" ? "wss://" : "ws://";
    const sock = new WebSocket(proto + window.location.host + "/api/fs/events?" + query);
    es = sock;
    sock.onmessage = (ev) => {
      let data;
      try {
        data = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      if (data.keepalive) return;
      // Any change (including deletion, mtime: null → LR-6) reloads after a
      // 300 ms debounce that coalesces bursts.
      clearTimeout(reloadTimer);
      reloadTimer = setTimeout(() => window.location.reload(), 300);
    };
    // Unlike EventSource, a WebSocket doesn't reconnect itself — retry unless
    // this close was deliberate (es already points elsewhere / is null).
    sock.onclose = () => {
      if (es !== sock) return;
      es = null;
      clearTimeout(resubscribeTimer);
      resubscribeTimer = setTimeout(resubscribe, 1000);
    };
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
    const pre = document.createElement("pre");
    pre.style.cssText = "margin:0;white-space:pre-wrap;word-break:break-word;";
    pre.textContent = err.traceback || "";
    overlay.appendChild(title);
    overlay.appendChild(pre);
    document.body.appendChild(overlay);
  }

  window.addEventListener("unhandledrejection", (event) => {
    const err = event.reason;
    // A superseded/aborted runPython (D113) rejects with a benign AbortError:
    // swallow it so a fire-and-forget re-render that lost the race neither shows
    // the overlay nor logs an "uncaught (in promise)" to the console.
    if (err && err.name === "AbortError") {
      event.preventDefault();
      return;
    }
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
