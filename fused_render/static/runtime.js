/*
 * Injected into every rendered HTML file (see server.py `/render`).
 * Provides `window.fused`:
 *   fused.runPython(pyPath, params) -> Promise<result>
 *   fused.params.get(key) / getAll() / set(key, value) / onChange(cb) -> unsubscribe
 *
 * Same-origin iframe model: this script talks to `window.parent`'s URL directly
 * (no postMessage bridge — see DECISIONS.md D3/D4). When loaded as a top-level
 * page (parent === window, e.g. visiting /render?path=... directly) it falls
 * back to reading/writing its own URL, treating the `path` query key as
 * reserved alongside any `_`-prefixed key.
 */
(function () {
  "use strict";

  const standalone = window.parent === window;
  const target = standalone ? window : window.parent;

  function isReserved(key) {
    if (key.startsWith("_")) return true;
    if (standalone && key === "path") return true;
    return false;
  }

  function currentParams() {
    return new URLSearchParams(target.location.search);
  }

  const listeners = new Set();

  function notify() {
    const snapshot = getAll();
    for (const cb of listeners) {
      try {
        cb(snapshot);
      } catch (e) {
        console.error("[fused] params.onChange listener threw:", e);
      }
    }
  }

  function get(key) {
    const params = currentParams();
    if (key === "_file") {
      return params.has("_file") ? params.get("_file") : undefined;
    }
    if (isReserved(key)) return undefined;
    return params.has(key) ? params.get(key) : undefined;
  }

  function getAll() {
    const params = currentParams();
    const result = {};
    for (const [key, value] of params) {
      if (key === "_file") {
        result[key] = value;
        continue;
      }
      if (isReserved(key)) continue;
      result[key] = value;
    }
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
    const params = currentParams();
    params.set(key, value);
    const search = params.toString();
    const newUrl = target.location.pathname + (search ? "?" + search : "");
    target.history.replaceState(target.history.state, "", newUrl);
    notify();
  }

  function onChange(cb) {
    listeners.add(cb);
    return () => listeners.delete(cb);
  }

  function runPython(pyPath, params) {
    const ownPath = new URLSearchParams(window.location.search).get("path");
    return fetch("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ py: pyPath, html: ownPath, params: params || {} }),
    })
      .then((res) => res.json())
      .then((data) => {
        if (data.stdout) {
          console.log("[python]", data.stdout);
        }
        if (!data.ok) {
          const err = new Error(data.error && data.error.message);
          err.type = data.error && data.error.type;
          err.traceback = data.error && data.error.traceback;
          err.stdout = data.stdout;
          throw err;
        }
        return data.result;
      });
  }

  window.fused = {
    runPython,
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
    if (err && err.traceback) {
      showOverlay(err);
    }
  });
})();
