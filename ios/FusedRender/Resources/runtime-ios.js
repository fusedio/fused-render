/*
 * fused runtime overrides for the native iOS shell (CaptureBridge.swift).
 *
 * Injected as a WKUserScript at document end — after the page's own
 * <script src="/static/runtime.js"> (the stock desktop runtime; lan.py serves
 * that to this shell's user agent) has built `window.fused`. Replaces the
 * members that mean something different on a phone: `fused.capture.*` becomes
 * a bridge to native recording (microphone, this screen, a snapshot) and
 * `fused.fileIndex` says it is unavailable. Every return SHAPE is the desktop
 * one (runtime.js: captureHandle, captureScreenshot's {path,url,width,height,
 * bytes,mime}, /api/capture's sources dict) — apps written for the desktop run
 * unchanged.
 *
 * Protocol: JS posts {id, op, args, page} to the `fusedCapture` handler and
 * keeps the promise in `pending`; Swift answers by calling
 * `window.__fusedBridge.resolve(id, value)` or `.reject(id, {message, type})`.
 */
(function () {
  const fused = window.fused;
  if (!fused || !window.webkit || !window.webkit.messageHandlers ||
      !window.webkit.messageHandlers.fusedCapture) return;

  fused.device = "ios-app";

  const pending = new Map();
  let seq = 0;

  function call(op, args) {
    return new Promise((resolve, reject) => {
      const id = "c" + (++seq) + "-" + Date.now().toString(36);
      pending.set(id, { resolve, reject });
      let page = null;
      try { page = new URLSearchParams(window.location.search).get("path"); } catch (e) {}
      window.webkit.messageHandlers.fusedCapture.postMessage({ id, op, args: args || {}, page });
    });
  }

  window.__fusedBridge = {
    resolve(id, value) {
      const p = pending.get(id);
      if (!p) return;
      pending.delete(id);
      p.resolve(value);
    },
    reject(id, err) {
      const p = pending.get(id);
      if (!p) return;
      pending.delete(id);
      const e = new Error((err && err.message) || "capture failed");
      e.type = (err && err.type) || "capture_error";
      p.reject(e);
    },
  };

  // The desktop handle (runtime.js captureHandle): id, mode, path, jobId,
  // maxSeconds, stop(), cancel(), state, url.
  function handle(started) {
    let state = "recording";
    let result = null;
    let ending = null;
    function end(action) {
      if (ending) return ending;
      ending = call(action, { id: started.id })
        .then((done) => {
          state = done.state || (action === "cancel" ? "cancelled" : "stopped");
          result = done;
          return done;
        })
        .catch((err) => { ending = null; state = "error"; throw err; });
      return ending;
    }
    const h = {
      id: started.id,
      mode: started.mode,
      path: started.path,
      jobId: null,
      maxSeconds: started.maxSeconds || null,
      stop: () => end("stop"),
      cancel: () => end("cancel"),
    };
    Object.defineProperty(h, "state", { get: () => state });
    Object.defineProperty(h, "url", { get: () => (result ? result.url : fused.rawUrl(started.path)) });
    return h;
  }

  fused.capture = {
    // fused.capture.audio({source, path, maxSeconds, title}) — the phone's mic.
    audio(opts) {
      opts = opts || {};
      if (opts.source && opts.source !== "mic") {
        return Promise.reject(Object.assign(
          new Error("only the microphone can be recorded on this device (source: \"mic\")"),
          { type: "capture_error" }));
      }
      return call("audio", opts).then(handle);
    },
    // fused.capture.screen({audio, path, maxSeconds, title}) — this app's
    // screen (ReplayKit); `audio: "mic"|"both"` adds the microphone.
    screen(opts) {
      return call("screen", opts || {}).then(handle);
    },
    // fused.capture.screenshot({path}) — a snapshot of this page.
    screenshot(opts) {
      return call("screenshot", opts || {});
    },
    sources() {
      return call("sources", {});
    },
    list() {
      return call("list", {});
    },
    attach() {
      return Promise.reject(Object.assign(
        new Error("recordings on this device end with the page's own stop(); nothing to attach to"),
        { type: "capture_error" }));
    },
  };

  if (fused.fileIndex) {
    const unavailable = () => Promise.reject(Object.assign(
      new Error("the file index is not available from a phone"), { type: "index_unavailable" }));
    fused.fileIndex = { search: unavailable, query: unavailable };
  }
})();
