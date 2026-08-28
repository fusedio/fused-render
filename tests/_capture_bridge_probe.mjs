/* Run runtime.js's capture bridge FOR REAL and report what order it did things
 * in. Same idea as `_git_view_probe.mjs`: a stub just large enough to run the
 * shipping code verbatim, with no copy of the logic under test.
 *
 * Why this exists: on Windows and Linux a recording is assembled by the page
 * (SPEC CP-10), and its correctness is almost entirely ORDER.
 *   - the share picker must open BEFORE the server allocates anything, or a
 *     cancelled picker leaves a job row over an empty file;
 *   - the socket must open BEFORE `recorder.start()`, or the first chunk is a
 *     hole in the middle of the container;
 *   - the last chunk must be sent BEFORE the `eos` frame;
 *   - `eos` must be answered BEFORE the stop request, which travels on another
 *     connection and would otherwise close the file ahead of the tail.
 * None of that is visible to Python, and `node --check` only proves the file
 * parses. Nothing else in the suite executes the bridge at all.
 *
 * It also pins the two things a page must never see: `sources().client` and the
 * handle's `transport`/`streamToken` (CP-8 — no field naming which backend
 * served you).
 *
 * If an UNRELATED runtime.js change breaks this stub, extend the stub — the
 * failure is real in the sense that the bridge stopped being runnable in
 * isolation, which is the property being protected.
 *
 * Usage: node _capture_bridge_probe.mjs <runtime.js>
 * Prints one JSON object: { keys, sources, handle, order, chunks, stop, error }.
 */
import { readFileSync } from "node:fs";

const [runtimePath] = process.argv.slice(2);
const src = readFileSync(runtimePath, "utf8");

const order = [];
const chunks = [];

class FakeBlob {
  constructor(bytes) {
    this.size = bytes.length;
    this._bytes = bytes;
  }
  // Async on purpose: it is why the bridge needs one promise chain rather than
  // one promise per chunk.
  arrayBuffer() {
    return Promise.resolve(this._bytes);
  }
}

class FakeRecorder {
  // Only mp4 is "supported", so the bridge must pick the mp4 container.
  static isTypeSupported(type) {
    return type.startsWith("video/mp4") || type.startsWith("audio/mp4");
  }
  constructor(stream, opts) {
    this.state = "inactive";
    this.mimeType = opts && opts.mimeType;
  }
  start(slice) {
    order.push("recorder.start:" + slice);
    this.state = "recording";
    setTimeout(() => {
      if (this.ondataavailable) {
        this.ondataavailable({ data: new FakeBlob(new Uint8Array([1, 2, 3])) });
      }
    }, 1);
  }
  stop() {
    order.push("recorder.stop");
    this.state = "inactive";
    if (this.ondataavailable) {
      this.ondataavailable({ data: new FakeBlob(new Uint8Array([9])) });
    }
    if (this.onstop) setTimeout(() => this.onstop(), 1);
  }
}

class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 1;
    // Only the capture socket is interesting; runtime.js also opens
    // /api/fs/events for its own reasons.
    if (url.indexOf("/api/capture/") !== -1) order.push("ws.open");
    setTimeout(() => this.onopen && this.onopen(), 0);
  }
  send(payload) {
    if (payload === "eos") {
      order.push("ws.eos");
      setTimeout(() => this.onmessage && this.onmessage({ data: "flushed" }), 0);
      return;
    }
    chunks.push(payload.byteLength === undefined
      ? payload.length : payload.byteLength);
    order.push("ws.chunk");
  }
  close() {
    if (this.url.indexOf("/api/capture/") !== -1) order.push("ws.close");
    this.readyState = 3;
  }
}

const videoTrack = { kind: "video", stop() {} };
const mediaStream = {
  getTracks: () => [videoTrack],
  getVideoTracks: () => [videoTrack],
  getAudioTracks: () => [],
};

const REPLIES = {
  "GET /api/capture": {
    sources: {
      client: true,
      video: { available: true, granted: true, reason: null },
      audio: { available: true, granted: true, reason: null },
      systemAudio: { available: true, reason: null },
      screenshot: { available: true, granted: true, reason: null },
      displays: [],
      microphones: [],
    },
    active: [],
  },
  "POST /api/capture/start": {
    id: "abc123", mode: "screen", path: "/tmp/x.mp4", state: "recording",
    jobId: "sys:capture:abc123", maxSeconds: 1800, audio: false,
    transport: "stream", streamToken: "tok-secret",
  },
  "POST /api/capture/abc123/stop": {
    id: "abc123", state: "stopped", path: "/tmp/x.mp4", seconds: 1.5,
    url: "/api/fs/raw?path=%2Ftmp%2Fx.mp4", bytes: 4, mime: "video/mp4",
  },
};

const element = () => ({
  style: { setProperty() {} }, classList: { add() {}, remove() {} },
  setAttribute() {}, getAttribute: () => null, hasAttribute: () => false,
  appendChild() {}, addEventListener() {}, remove() {},
});

const win = {
  location: {
    search: "?path=/tmp/page.html", protocol: "http:", host: "127.0.0.1:8000",
    href: "http://127.0.0.1:8000/", hash: "", pathname: "/",
  },
  document: {
    readyState: "complete", addEventListener() {}, removeEventListener() {},
    querySelectorAll: () => [], querySelector: () => null,
    getElementById: () => null, createElement: element,
    documentElement: element(), head: element(), body: element(),
  },
  matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
  addEventListener() {}, removeEventListener() {},
  history: { replaceState() {}, pushState() {} },
  MediaRecorder: FakeRecorder,
  WebSocket: FakeSocket,
  MediaStream: class {
    constructor(tracks) {
      this._tracks = tracks || [];
      this.getTracks = () => this._tracks;
      this.getVideoTracks = () => this._tracks;
      this.getAudioTracks = () => [];
    }
  },
  navigator: {
    mediaDevices: {
      getDisplayMedia: async () => {
        order.push("picker");
        return mediaStream;
      },
      getUserMedia: async () => mediaStream,
      enumerateDevices: async () => [
        // Label empty, as a browser reports before the permission is granted.
        { kind: "audioinput", deviceId: "default", label: "" },
        { kind: "videoinput", deviceId: "cam", label: "Camera" },
      ],
    },
  },
  setTimeout, clearTimeout,
  fetch: async (path, init) => {
    const key = (init && init.method ? init.method : "GET") + " " + path;
    if (path.indexOf("/api/capture") === 0) order.push("fetch " + key);
    const body = REPLIES[key] || {};
    // A FRESH object per call, like real JSON parsing: the probe merge mutates
    // what it is handed, and a shared literal would let one read see another's
    // deletions.
    return {
      ok: true, status: 200,
      json: async () => JSON.parse(JSON.stringify(body)),
    };
  },
};
win.window = win;
win.parent = win;
win.self = win;
win.top = win;

const out = { keys: null, sources: null, handle: null, order, chunks,
              stop: null, error: null };
try {
  // runtime.js is a classic script that reads free `window`/`document`/... —
  // handing them in as parameters runs it verbatim with no edit.
  const run = new Function(
    "window", "document", "location", "navigator", "fetch", "history",
    "setTimeout", "clearTimeout", "MediaRecorder", "WebSocket", "MediaStream",
    "matchMedia", src);
  run(win, win.document, win.location, win.navigator, win.fetch, win.history,
      setTimeout, clearTimeout, FakeRecorder, FakeSocket, win.MediaStream,
      win.matchMedia);

  const capture = win.fused.capture;
  out.keys = Object.keys(capture).sort();

  const sources = await capture.sources();
  out.sources = {
    clientStripped: !("client" in sources),
    video: sources.video,
    microphones: sources.microphones,
  };

  const rec = await capture.screen({ audio: false, maxSeconds: 1800 });
  out.handle = {
    keys: Object.keys(rec).sort(),
    path: rec.path,
    jobId: rec.jobId,
    leaks: ["transport", "streamToken"].filter((key) => key in rec),
  };

  await new Promise((done) => setTimeout(done, 25));
  out.stop = await rec.stop();
} catch (err) {
  out.error = (err && err.stack) || String(err);
}
console.log(JSON.stringify(out));
