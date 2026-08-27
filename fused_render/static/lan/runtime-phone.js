/*
 * Phone-browser overrides for the fused runtime (fused_render/lan.py).
 *
 * Served ONLY to the local-network listener: LanApp answers GET /static/runtime.js
 * with the stock runtime.js followed by this file, so a page opened from a phone
 * gets the same `window.fused` with a few members replaced. The desktop keeps
 * the stock file untouched. Nothing here changes a return SHAPE — the desktop
 * contract (runtime.js: captureHandle, captureScreenshot's {path,url,width,
 * height,bytes,mime}, /api/capture's sources dict) is the ABI apps wrote against,
 * and a future native iOS shell replaces this file with a bridge that keeps it.
 *
 * WHY THESE THREE ARE DIFFERENT ON A PHONE. The stock fused.capture.* records
 * the LAPTOP — its displays, its microphone, through the server's native
 * capture — which is nonsense from a phone (and the LAN wrapper refuses
 * /api/capture anyway). A phone browser has no screen-capture API at all (iOS
 * Safari lacks getDisplayMedia), and over plain http it withholds getUserMedia
 * (insecure context; see lan.py for why it is http). What it DOES have is the
 * camera and the photo library through <input type="file">, so:
 *
 *   capture.audio()      -> the camera records a short VIDEO (with the phone
 *                           mic), uploaded as-is. Downstream that is what the
 *                           caller wanted: fused.ai.transcribe accepts video.
 *                           The result says mime video/mp4, honestly.
 *   capture.screen()     -> the user picks a Control-Center screen recording
 *                           (or any video) from Photos; uploaded as-is.
 *   capture.screenshot() -> a DOM snapshot of THIS page (.html, images inlined
 *                           where same-origin), the only picture of itself a
 *                           browser page can take without a capture API.
 *   capture.sources()    -> the truthful answer for this device: no displays,
 *                           no native mics; `client: "phone-web"`.
 *
 * Files land beside the app (`<app dir>/captures/`) unless `path` says
 * otherwise — the only folders the LAN side may write are the app's own and
 * the state dir (lan.allowed_roots), so a laptop-style default like
 * ~/recordings would be refused.
 */
(function () {
  const fused = window.fused;
  if (!fused) return;

  fused.device = "phone-web";

  const ownPath = () => {
    try { return new URLSearchParams(window.location.search).get("path") || ""; }
    catch (e) { return ""; }
  };
  const dirname = (p) => p.replace(/[\\/][^\\/]*$/, "");
  const stamp = () => new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
  const extOf = (file, fallback) => {
    const m = /\.([A-Za-z0-9]+)$/.exec(file && file.name || "");
    return m ? m[1].toLowerCase() : fallback;
  };
  const defaultPath = (kind, file, fallbackExt) => {
    const dir = dirname(ownPath()) || "";
    return dir + "/captures/" + kind + "-" + stamp() + "." + extOf(file, fallbackExt);
  };
  const mimeOf = (path, file) => {
    if (file && file.type) return file.type;
    const ext = path.split(".").pop().toLowerCase();
    return { mov: "video/quicktime", mp4: "video/mp4", webm: "video/webm", m4a: "audio/mp4",
             png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", html: "text/html" }[ext]
           || "application/octet-stream";
  };

  // One <input type="file"> per call: the ONLY way a page on plain http reaches
  // the camera or the library. Resolves with the File, or rejects when the
  // picker closes with nothing (iOS fires no `cancel`; a focus return with an
  // empty list is the signal).
  function pickFile(accept, capture) {
    return new Promise((resolve, reject) => {
      const input = document.createElement("input");
      input.type = "file";
      input.accept = accept;
      if (capture) input.setAttribute("capture", capture);
      input.style.cssText = "position:fixed;left:-9999px;top:0;width:1px;height:1px;opacity:0";
      let settled = false;
      const finish = (file) => {
        if (settled) return;
        settled = true;
        window.removeEventListener("focus", onFocus, true);
        input.remove();
        if (file) resolve(file);
        else {
          const err = new Error("nothing was recorded or picked");
          err.type = "capture_cancelled";
          reject(err);
        }
      };
      const onFocus = () => setTimeout(() => finish(input.files && input.files[0]), 600);
      input.addEventListener("change", () => finish(input.files && input.files[0]));
      window.addEventListener("focus", onFocus, true);
      document.body.appendChild(input);
      input.click();
    });
  }

  // A handle with the desktop shape (captureHandle in runtime.js), for a
  // recording that is ALREADY finished by the time the picker returns: `stop()`
  // resolves with the stored file, `cancel()` throws capture_cancelled. `state`
  // starts as "recording" while the picker is open — the page's UI is written
  // for that — and settles when the upload lands.
  function finishedHandle(mode, path, uploading) {
    let state = "recording";
    let result = null;
    const done = uploading.then((r) => { state = "stopped"; result = r; return r; },
                                (e) => { state = e.type === "capture_cancelled" ? "cancelled" : "error"; throw e; });
    const handle = {
      id: "phone-" + mode + "-" + Date.now().toString(36),
      mode,
      path,
      jobId: null,
      maxSeconds: null,
      stop: () => done,
      cancel: () => done.then(() => {
        const err = new Error("already recorded on this device — delete the file instead");
        err.type = "capture_error";
        throw err;
      }),
    };
    Object.defineProperty(handle, "state", { get: () => state });
    Object.defineProperty(handle, "url", { get: () => (result ? result.url : fused.rawUrl(path)) });
    return handle;
  }

  async function store(file, path) {
    await fused.uploadFile(path, file);
    return { path, url: fused.rawUrl(path), bytes: file.size, mime: mimeOf(path, file),
             state: "stopped", device: "phone-web" };
  }

  // fused.capture.audio({path, title}) — the camera records video WITH the mic.
  // `source` other than "mic" is refused as on the desktop.
  function captureAudio(opts) {
    opts = opts || {};
    if (opts.source && opts.source !== "mic") {
      return Promise.reject(Object.assign(
        new Error("only the microphone can be recorded on a phone (source: \"mic\")"),
        { type: "capture_error" }));
    }
    const picked = pickFile("video/*", "user");
    let path = opts.path || null;
    const uploading = picked.then((file) => store(file, path || (path = defaultPath("audio", file, "mp4"))));
    // The handle needs `path` synchronously; without one from the caller it is
    // known only once the file (and its extension) is — so expose the default
    // stem now and let the stored result carry the real path.
    return Promise.resolve(finishedHandle("audio", path || defaultPath("audio", null, "mp4"), uploading));
  }

  // fused.capture.screen({path, title}) — pick a screen recording from Photos.
  function captureScreen(opts) {
    opts = opts || {};
    const picked = pickFile("video/*", null);
    let path = opts.path || null;
    const uploading = picked.then((file) => store(file, path || (path = defaultPath("screen", file, "mp4"))));
    return Promise.resolve(finishedHandle("screen", path || defaultPath("screen", null, "mp4"), uploading));
  }

  // fused.capture.screenshot({path}) — a DOM snapshot of this page as .html.
  async function captureScreenshot(opts) {
    opts = opts || {};
    const doc = document.documentElement.cloneNode(true);
    // Inline same-origin <img> bitmaps so the snapshot stands alone; a
    // cross-origin one stays a URL (a tainted canvas cannot be read).
    const live = document.querySelectorAll("img");
    const copies = doc.querySelectorAll("img");
    for (let i = 0; i < live.length && i < copies.length; i++) {
      const img = live[i];
      if (!img.complete || !img.naturalWidth) continue;
      try {
        const c = document.createElement("canvas");
        c.width = img.naturalWidth; c.height = img.naturalHeight;
        c.getContext("2d").drawImage(img, 0, 0);
        copies[i].setAttribute("src", c.toDataURL("image/png"));
      } catch (e) { /* cross-origin: keep the URL */ }
    }
    // Scripts do not belong in a picture; a snapshot is for looking at.
    doc.querySelectorAll("script").forEach((s) => s.remove());
    const base = document.createElement("base");
    base.href = location.href;
    (doc.querySelector("head") || doc).prepend(base);
    const html = "<!doctype html>\n" + doc.outerHTML;
    const path = opts.path || defaultPath("screenshot", null, "html");
    await fused.writeFile(path, html);
    return { path, url: fused.rawUrl(path), width: window.innerWidth, height: window.innerHeight,
             bytes: new Blob([html]).size, mime: "text/html", device: "phone-web" };
  }

  // fused.capture.sources() — what THIS device can do, in the server's shape.
  function captureSources() {
    const why = "this is a phone browser: it records through the camera (capture.audio / "
              + "capture.screen open the camera or the photo library) and snapshots the page as HTML";
    return Promise.resolve({
      client: undefined,
      device: "phone-web",
      native: false,
      displays: [],
      microphones: [],
      video: { available: false, granted: false, reason: why },
      audio: { available: true, granted: true, reason: "camera video carries the microphone" },
      systemAudio: { available: false, reason: why },
      screenshot: { available: true, kind: "dom-snapshot" },
    });
  }

  fused.capture = {
    screen: captureScreen,
    audio: captureAudio,
    screenshot: captureScreenshot,
    sources: captureSources,
    // No server-side recordings can belong to this device.
    list: () => Promise.resolve([]),
    attach: () => Promise.reject(Object.assign(
      new Error("recordings on a phone finish when the picker closes; nothing to attach to"),
      { type: "capture_error" })),
  };

  // The file index is a laptop-wide read the LAN side refuses (lan.py); answer
  // in the runtime's own shape so a page degrades instead of surfacing a 404.
  if (fused.fileIndex) {
    const unavailable = () => Promise.reject(Object.assign(
      new Error("the file index is not available from a phone"), { type: "index_unavailable" }));
    fused.fileIndex = { search: unavailable, query: unavailable };
  }
})();
