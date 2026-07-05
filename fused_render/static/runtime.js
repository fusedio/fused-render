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

  window.fused = {
    runPython,
    rawUrl,
    stat,
    readFile,
    writeFile,
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
