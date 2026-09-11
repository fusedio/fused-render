// The two backends behind one handle, with a fake `/api/capture` and a fake
// MediaRecorder realm. What is under test is the DECISION SEQUENCE — which
// route is called, in what order, and what is undone when a step fails — not
// the browser's encoder.
import { describe, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

const { audioType, captureAudio, captureSources, mergeSources } = await import("./capture-audio");
type CaptureHost = import("./capture-audio").CaptureHost;
type CaptureError = import("./capture-audio").CaptureError;

// ── the server ─────────────────────────────────────────────────────────────

interface Call {
  url: string;
  method: string | undefined;
  body: unknown;
}

function fakeFetch(routes: Record<string, unknown | (() => unknown)>, calls: Call[]) {
  return ((url: string, init?: RequestInit) => {
    calls.push({
      url,
      method: init?.method,
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    });
    const hit = routes[url];
    if (hit === undefined) {
      return Promise.resolve({
        ok: false,
        status: 404,
        statusText: "Not Found",
        json: () => Promise.resolve({ error: "no such route: " + url }),
      } as Response);
    }
    const value = typeof hit === "function" ? (hit as () => unknown)() : hit;
    const v = value as { _status?: number; _error?: string };
    if (v && v._status) {
      return Promise.resolve({
        ok: false,
        status: v._status,
        statusText: "",
        json: () => Promise.resolve({ error: v._error }),
      } as Response);
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      statusText: "OK",
      json: () => Promise.resolve(value),
    } as Response);
  }) as unknown as typeof fetch;
}

const nativeStart = {
  id: "c1",
  mode: "audio",
  path: "/Users/x/recordings/2026-09-08.m4a",
  state: "recording",
  seconds: 0,
  maxSeconds: 1800,
  jobId: "capture:c1",
};

const stopped = {
  id: "c1",
  mode: "audio",
  state: "stopped",
  path: nativeStart.path,
  url: "/api/fs/raw?path=x",
  seconds: 12.5,
  bytes: 91234,
  mime: "audio/mp4",
  maxSeconds: 1800,
  jobId: "capture:c1",
};

// ── the browser realm ──────────────────────────────────────────────────────

interface FakeRecorderLog {
  started: number[];
  chunks: ArrayBuffer[];
  sent: unknown[];
  stopped: number;
}

function fakeHost(
  log: FakeRecorderLog,
  opts: { types?: string[]; mic?: boolean; socketRefuses?: string } = {},
): CaptureHost {
  const types = opts.types ?? ['audio/mp4;codecs="mp4a.40.2"'];
  class Recorder {
    static isTypeSupported = (t: string) => types.includes(t);
    state = "inactive";
    ondataavailable: ((e: { data: { size: number; arrayBuffer(): Promise<ArrayBuffer> } }) => void) | null = null;
    onstop: (() => void) | null = null;
    constructor(
      public stream: unknown,
      public options: { mimeType: string },
    ) {}
    start(slice: number) {
      log.started.push(slice);
      this.state = "recording";
    }
    stop() {
      log.stopped += 1;
      this.state = "inactive";
      if (this.onstop) this.onstop();
    }
  }
  class Sock {
    static OPEN = 1;
    binaryType = "";
    readyState = 1;
    onopen: (() => void) | null = null;
    onclose: ((e: { reason?: string }) => void) | null = null;
    onerror: (() => void) | null = null;
    onmessage: ((e: { data: unknown }) => void) | null = null;
    constructor(public url: string) {
      setTimeout(() => {
        if (opts.socketRefuses) {
          this.readyState = 3;
          if (this.onclose) this.onclose({ reason: opts.socketRefuses });
        } else if (this.onopen) this.onopen();
      }, 0);
    }
    send(payload: unknown) {
      log.sent.push(payload);
      if (payload === "eos" && this.onmessage) this.onmessage({ data: "flushed" });
    }
    close() {
      this.readyState = 3;
    }
  }
  const track = { stop: () => {}, kind: "audio" };
  const stream = { getTracks: () => [track], getAudioTracks: () => [track] };
  return {
    MediaRecorder: Recorder as unknown as typeof MediaRecorder,
    WebSocket: Sock as unknown as typeof WebSocket,
    navigator: {
      mediaDevices: {
        getUserMedia: () =>
          opts.mic === false
            ? Promise.reject(Object.assign(new Error("denied"), { name: "NotAllowedError" }))
            : Promise.resolve(stream as unknown as MediaStream),
        enumerateDevices: () =>
          Promise.resolve([
            { kind: "audioinput", deviceId: "default", label: "" },
            { kind: "audioinput", deviceId: "usb-1", label: "Yeti" },
            { kind: "videoinput", deviceId: "cam", label: "FaceTime" },
          ] as MediaDeviceInfo[]),
      },
    },
    location: { protocol: "http:", host: "127.0.0.1:2449" },
  };
}

const emptyLog = (): FakeRecorderLog => ({ started: [], chunks: [], sent: [], stopped: 0 });

// ── the native backend (macOS) ─────────────────────────────────────────────

describe("captureAudio, native backend", () => {
  test("one GET to learn the fork, then start — and the path is known before the first sample", async () => {
    const calls: Call[] = [];
    const rec = await captureAudio(
      { title: "Spoken walkthrough" },
      {
        fetch: fakeFetch(
          {
            "/api/capture": { sources: { audio: { available: true, reason: null } } },
            "/api/capture/start": nativeStart,
          },
          calls,
        ),
      },
    );
    expect(calls.map((c) => c.url)).toEqual(["/api/capture", "/api/capture/start"]);
    expect(calls[0].method).toBe("GET");
    // No `path` and no `container` of ours: the backend names the file (CP-5).
    expect(calls[1].body).toEqual({ mode: "audio", title: "Spoken walkthrough" });
    expect(rec.path).toBe(nativeStart.path);
    expect(rec.jobId).toBe("capture:c1");
    expect(rec.state).toBe("recording");
  });

  test("stop KEEPS the file and reports its seconds and bytes", async () => {
    const calls: Call[] = [];
    const rec = await captureAudio(
      {},
      {
        fetch: fakeFetch(
          {
            "/api/capture": { sources: {} },
            "/api/capture/start": nativeStart,
            "/api/capture/c1/stop": stopped,
          },
          calls,
        ),
      },
    );
    const out = await rec.stop();
    expect(out.seconds).toBe(12.5);
    expect(out.bytes).toBe(91234);
    expect(out.path).toBe(nativeStart.path);
    expect(rec.state).toBe("stopped");
    expect(rec.url).toBe("/api/fs/raw?path=x");
  });

  test("cancel STOPS AND DELETES — the ✕'s meaning (CP-4)", async () => {
    const calls: Call[] = [];
    const rec = await captureAudio(
      {},
      {
        fetch: fakeFetch(
          {
            "/api/capture": { sources: {} },
            "/api/capture/start": nativeStart,
            "/api/capture/c1/cancel": { ...stopped, state: "cancelled", path: null, url: null },
          },
          calls,
        ),
      },
    );
    const out = await rec.cancel();
    expect(out.path).toBeNull();
    expect(rec.state).toBe("cancelled");
    expect(calls.map((c) => c.url)).toContain("/api/capture/c1/cancel");
  });

  test("the ENDING IS MEMOIZED: a double-clicked stop makes ONE request (R:5295)", async () => {
    const calls: Call[] = [];
    const rec = await captureAudio(
      {},
      {
        fetch: fakeFetch(
          {
            "/api/capture": { sources: {} },
            "/api/capture/start": nativeStart,
            "/api/capture/c1/stop": stopped,
          },
          calls,
        ),
      },
    );
    const [a, b] = await Promise.all([rec.stop(), rec.stop()]);
    expect(a).toBe(b);
    expect(calls.filter((c) => c.url === "/api/capture/c1/stop")).toHaveLength(1);
  });

  test("a stop racing a cancel gets the CANCEL's promise back, not a second request", async () => {
    const calls: Call[] = [];
    const rec = await captureAudio(
      {},
      {
        fetch: fakeFetch(
          {
            "/api/capture": { sources: {} },
            "/api/capture/start": nativeStart,
            "/api/capture/c1/cancel": { ...stopped, state: "cancelled", path: null },
          },
          calls,
        ),
      },
    );
    const going = rec.cancel();
    await rec.stop();
    await going;
    expect(calls.map((c) => c.url).filter((u) => u.startsWith("/api/capture/c1"))).toEqual([
      "/api/capture/c1/cancel",
    ]);
  });

  test("a stop whose FILE failed to write reports the failure, not a path to something unplayable", async () => {
    const rec = await captureAudio(
      {},
      {
        fetch: fakeFetch(
          {
            "/api/capture": { sources: {} },
            "/api/capture/start": nativeStart,
            "/api/capture/c1/stop": { ...stopped, state: "error", error: "OSError: disk full" },
          },
          [],
        ),
      },
    );
    const err = (await rec.stop().catch((e: CaptureError) => e)) as CaptureError;
    expect(err.message).toBe("OSError: disk full");
    expect(err.type).toBe("capture_error");
  });

  test("a 409 is `unavailable` and carries the machine's own sentence (R:4986)", async () => {
    const err = (await captureAudio(
      {},
      {
        fetch: fakeFetch(
          {
            "/api/capture": { sources: {} },
            "/api/capture/start": {
              _status: 409,
              _error: "native capture needs macOS 15",
            },
          },
          [],
        ),
      },
    ).catch((e: CaptureError) => e)) as CaptureError;
    expect(err.message).toBe("native capture needs macOS 15");
    expect(err.type).toBe("unavailable");
  });

  test("a 400 is `bad_request` — the same split /api/ai/* makes", async () => {
    const err = (await captureAudio(
      { device: "usb-1" },
      {
        fetch: fakeFetch(
          {
            "/api/capture": { sources: {} },
            "/api/capture/start": {
              _status: 400,
              _error: "'device' is a screen recording's option",
            },
          },
          [],
        ),
      },
    ).catch((e: CaptureError) => e)) as CaptureError;
    expect(err.type).toBe("bad_request");
    expect(err.message).toBe("'device' is a screen recording's option");
  });

  test("`base` rides the start only when the caller has a page path (RH-1)", async () => {
    const calls: Call[] = [];
    await captureAudio(
      { path: "clip.m4a" },
      {
        base: "/Users/x/app/page.html",
        fetch: fakeFetch(
          { "/api/capture": { sources: {} }, "/api/capture/start": nativeStart },
          calls,
        ),
      },
    );
    expect(calls[1].body).toEqual({
      mode: "audio",
      path: "clip.m4a",
      base: "/Users/x/app/page.html",
    });
  });
});

// ── the streamed backend (Windows / Linux) ─────────────────────────────────

describe("captureAudio, browser-records backend", () => {
  test("media FIRST, then the row, then the socket, then the encoder (R:5247)", async () => {
    const calls: Call[] = [];
    const log = emptyLog();
    const rec = await captureAudio(
      { title: "Spoken walkthrough" },
      {
        host: fakeHost(log),
        fetch: fakeFetch(
          {
            "/api/capture": {
              sources: { client: true, audio: { available: true, reason: null } },
            },
            "/api/capture/start": nativeStart,
          },
          calls,
        ),
      },
    );
    // The CONTAINER is chosen here and sent — the encoder is in this browser,
    // so only this browser knows what it can write (R:5265).
    expect(calls[1].body).toEqual({
      mode: "audio",
      title: "Spoken walkthrough",
      container: "mp4",
    });
    expect(log.started).toEqual([1000]);
    expect(rec.path).toBe(nativeStart.path);
  });

  test("mp4 before webm — a fragmented mp4 needs no remux (R:5018)", () => {
    const log = emptyLog();
    expect(audioType(fakeHost(log))).toEqual({
      container: "mp4",
      mimeType: 'audio/mp4;codecs="mp4a.40.2"',
    });
    expect(audioType(fakeHost(log, { types: ["audio/webm;codecs=opus"] }))).toEqual({
      container: "webm",
      mimeType: "audio/webm;codecs=opus",
    });
    expect(audioType(fakeHost(log, { types: [] }))).toBeNull();
  });

  test("a browser with no storable container REFUSES, naming one that can (R:5253)", async () => {
    const err = (await captureAudio(
      {},
      {
        host: fakeHost(emptyLog(), { types: [] }),
        fetch: fakeFetch({ "/api/capture": { sources: { client: true } } }, []),
      },
    ).catch((e: CaptureError) => e)) as CaptureError;
    expect(err.type).toBe("unavailable");
    expect(err.message).toBe(
      "this browser cannot record: it has no MediaRecorder with a container " +
        "this app can store. Chrome, Edge or a recent Firefox can",
    );
  });

  test("a DENIED microphone leaves NO job row and no empty file (R:5079)", async () => {
    const calls: Call[] = [];
    const err = (await captureAudio(
      {},
      {
        host: fakeHost(emptyLog(), { mic: false }),
        fetch: fakeFetch(
          { "/api/capture": { sources: { client: true } }, "/api/capture/start": nativeStart },
          calls,
        ),
      },
    ).catch((e: CaptureError) => e)) as CaptureError;
    expect(calls.map((c) => c.url)).toEqual(["/api/capture"]);
    expect(err.type).toBe("bad_request");
    expect(err.message).toBe(
      "the capture was not allowed — the share dialog was dismissed or " +
        "the permission was denied",
    );
  });

  test("a socket the server REFUSES cancels the row rather than leaking it (R:5273)", async () => {
    const calls: Call[] = [];
    const err = (await captureAudio(
      {},
      {
        host: fakeHost(emptyLog(), { socketRefuses: "token mismatch" }),
        fetch: fakeFetch(
          {
            "/api/capture": { sources: { client: true } },
            "/api/capture/start": nativeStart,
            "/api/capture/c1/cancel": { ...stopped, state: "cancelled" },
          },
          calls,
        ),
      },
    ).catch((e: CaptureError) => e)) as CaptureError;
    expect(err.message).toBe("token mismatch");
    expect(calls.map((c) => c.url)).toContain("/api/capture/c1/cancel");
  });

  test("the stop FLUSHES first: `eos` is acknowledged before the request lands (R:5214)", async () => {
    const calls: Call[] = [];
    const log = emptyLog();
    const rec = await captureAudio(
      {},
      {
        host: fakeHost(log),
        fetch: fakeFetch(
          {
            "/api/capture": { sources: { client: true } },
            "/api/capture/start": nativeStart,
            "/api/capture/c1/stop": stopped,
          },
          calls,
        ),
      },
    );
    await rec.stop();
    expect(log.stopped).toBe(1);
    expect(log.sent).toEqual(["eos"]);
    // The recorder was stopped and the socket acknowledged BEFORE the request.
    expect(calls[calls.length - 1].url).toBe("/api/capture/c1/stop");
  });

  test("an unavailable machine rejects with the SERVER's sentence, no picker opened (R:5391)", async () => {
    const log = emptyLog();
    const err = (await captureAudio(
      {},
      {
        host: fakeHost(log),
        fetch: fakeFetch(
          {
            "/api/capture": {
              sources: {
                client: true,
                audio: { available: false, reason: "no input device is connected" },
              },
            },
          },
          [],
        ),
      },
    ).catch((e: CaptureError) => e)) as CaptureError;
    expect(err.message).toBe("no input device is connected");
    expect(err.type).toBe("unavailable");
    expect(log.started).toEqual([]);
  });
});

// ── sources() ──────────────────────────────────────────────────────────────

describe("captureSources", () => {
  test("macOS answers straight through — never prompting (CP-7)", async () => {
    const calls: Call[] = [];
    const out = await captureSources({
      fetch: fakeFetch(
        {
          "/api/capture": {
            sources: {
              audio: { available: true, granted: false, reason: null },
              screenshot: { available: true, reason: null },
            },
          },
        },
        calls,
      ),
    });
    expect(calls).toHaveLength(1);
    expect(out.audio).toEqual({ available: true, granted: false, reason: null });
    expect(out.screenshot).toEqual({ available: true, reason: null });
  });

  test("`client` is DELETED and the audio row answered from THIS browser (CP-8, R:5461)", async () => {
    const merged = (await mergeSources(
      { client: true, audio: { available: true, reason: null } },
      fakeHost(emptyLog()),
    )) as Record<string, unknown>;
    expect("client" in merged).toBe(false);
    expect(merged.audio).toEqual({ available: true, granted: true, reason: null });
  });

  test("a browser that cannot record loses the seat, with a reason naming one that can (CP-11)", async () => {
    const merged = await mergeSources(
      { client: true, audio: { available: true, reason: null } },
      fakeHost(emptyLog(), { types: [] }),
    );
    expect(merged.audio?.available).toBe(false);
    expect(merged.audio?.reason).toBe(
      "this browser cannot record — it has no MediaRecorder with a container " +
        "this app can store. Chrome, Edge or a recent Firefox can",
    );
  });

  test("the microphone list is audio inputs only, and an unlabelled one is numbered (R:5480)", async () => {
    const merged = await mergeSources({ client: true }, fakeHost(emptyLog()));
    expect(merged.microphones).toEqual([
      { id: "default", name: "Microphone 1", default: true },
      { id: "usb-1", name: "Yeti", default: false },
    ]);
  });
});
