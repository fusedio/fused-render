// THE CROSS-ORIGIN PANE'S DECISION POINTS (T:9706-9764). `capture.test.ts`
// injects `opts.strategies`, so the real tab-share strategy never runs there.
// What is under test HERE is the share CONTRACT — which constraints are asked
// for, how often the prompt is paid, when the stream is released — and the crop
// arithmetic, asserted as `drawImage`'s argument values rather than as pixels.
import { afterAll, beforeEach, describe, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

// `bun test` runs every file in ONE process against one `globalThis`, so
// whatever this suite stubs is put back the moment it is done — a leaked
// `navigator` or `document` breaks whoever runs next (the idiom
// platform/ui/appdoctor-lib.test.ts sets out and capture.test.ts follows).
const G = globalThis as Record<string, unknown>;
const BEFORE = {
  navigator: G.navigator,
  document: G.document,
};
afterAll(() => {
  G.navigator = BEFORE.navigator;
  G.document = BEFORE.document;
});

const { captureXO, currentStream, getStream, stopStream, watchStreamTeardown } = await import(
  "./xo-capture"
);

// ── the fakes ───────────────────────────────────────────────────────────────

type Rec = Record<string, unknown>;

interface FakeTrack {
  readyState: string;
  stops: number;
  listeners: unknown[][];
  stop: () => void;
  addEventListener: (...a: unknown[]) => void;
}

function track(readyState = "live", throwOnStop = false): FakeTrack {
  const t: FakeTrack = {
    readyState,
    stops: 0,
    listeners: [],
    stop: () => {
      t.stops++;
      if (throwOnStop) throw new Error("already ended");
    },
    addEventListener: (...a: unknown[]) => t.listeners.push(a),
  };
  return t;
}

interface FakeStream {
  tracks: FakeTrack[];
  getTracks: () => FakeTrack[];
  getVideoTracks: () => FakeTrack[];
}

function stream(...tracks: FakeTrack[]): FakeStream {
  return { tracks, getTracks: () => tracks, getVideoTracks: () => tracks };
}

interface FakeVideo {
  muted: boolean;
  srcObject: unknown;
  play: () => Promise<void>;
  requestVideoFrameCallback?: (cb: () => void) => void;
  videoWidth: number;
  videoHeight: number;
}

interface FakeCanvas {
  width: number;
  height: number;
  getContext: () => { drawImage: (...args: unknown[]) => void };
}

function rect(left: number, top: number, width: number, height: number): DOMRect {
  return {
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    x: left,
    y: top,
  } as DOMRect;
}

function hostWindow(innerWidth = 1000, innerHeight = 600): Window {
  return { innerWidth, innerHeight } as unknown as Window;
}

function makeFrame(r: DOMRect, win: Window | null = hostWindow()): HTMLIFrameElement {
  return {
    getBoundingClientRect: () => r,
    ownerDocument: { defaultView: win },
  } as unknown as HTMLIFrameElement;
}

let prompts: Rec[] = [];
let drawn: unknown[][] = [];
let canvases: FakeCanvas[] = [];
let videos: FakeVideo[] = [];
/** No `requestVideoFrameCallback` exercises the 150ms setTimeout fallback; a
 *  function exercises the "one PAINTED frame" road. */
let hasVfc = true;
let nextStream: () => FakeStream = () => stream(track());

function newVideo(): FakeVideo {
  const v: FakeVideo = {
    muted: false,
    srcObject: undefined,
    play: () => Promise.resolve(),
    videoWidth: 2000,
    videoHeight: 1200,
  };
  if (hasVfc) v.requestVideoFrameCallback = (cb: () => void) => cb();
  videos.push(v);
  return v;
}

function newCanvas(): FakeCanvas {
  const c: FakeCanvas = {
    width: 0,
    height: 0,
    getContext: () => ({ drawImage: (...args: unknown[]) => drawn.push(args) }),
  };
  canvases.push(c);
  return c;
}

function installNavigator(over?: Rec): void {
  G.navigator =
    over ||
    ({
      mediaDevices: {
        getDisplayMedia: (c: Rec) => {
          prompts.push(c);
          return Promise.resolve(nextStream());
        },
      },
    } as Rec);
}

beforeEach(() => {
  // The kept stream is module-level state and the whole point of the module, so
  // it must never leak into the next test — a live leftover would make a test
  // that expects a fresh prompt silently reuse the previous one.
  stopStream();
  prompts = [];
  drawn = [];
  canvases = [];
  videos = [];
  hasVfc = true;
  nextStream = () => stream(track());
  G.document = {
    hidden: false,
    addEventListener() {},
    removeEventListener() {},
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (tag: string) => (tag === "canvas" ? newCanvas() : newVideo()),
  };
  installNavigator();
});

// ── getStream ───────────────────────────────────────────────────────────────

describe("getStream constraints (T:9706)", () => {
  test("the current-tab hints that make the right choice the one-click one", async () => {
    // The crop is only ever computed against THIS tab's own layout, so any other
    // surface would be cropped wrong: the hints preselect this tab and offer no
    // switching and no monitors (ignored, not fatal, where unsupported).
    await getStream();
    expect(prompts.length).toBe(1);
    expect(prompts[0]).toEqual({
      video: { displaySurface: "browser" },
      audio: false,
      preferCurrentTab: true,
      selfBrowserSurface: "include",
      surfaceSwitching: "exclude",
      monitorTypeSurfaces: "exclude",
    });
  });

  test("a LIVE kept stream is reused — the prompt is paid once, not once per note", async () => {
    // A walkthrough clicking ten spots must not raise ten pickers.
    const first = await getStream();
    const second = await getStream();
    expect(second).toBe(first);
    expect(prompts.length).toBe(1);
  });

  test("a dead track means the share is gone: a fresh one is asked for", async () => {
    const dead = track("ended");
    nextStream = () => stream(dead);
    const first = await getStream();
    nextStream = () => stream(track("live"));
    const second = await getStream();
    expect(second).not.toBe(first);
    expect(prompts.length).toBe(2);
    // And the corpse is stopped on the way out rather than left held.
    expect(dead.stops).toBe(1);
  });

  test("the `ended` listener releases the kept stream", async () => {
    // The user ending the share from the browser's own indicator has to leave the
    // module with nothing held, or the next capture reuses a dead track.
    const t = track();
    nextStream = () => stream(t);
    await getStream();
    expect(currentStream()).not.toBeNull();
    expect(t.listeners[0][0]).toBe("ended");
    expect(t.listeners[0][2]).toEqual({ once: true });
    (t.listeners[0][1] as () => void)();
    expect(currentStream()).toBeNull();
  });
});

describe("stopStream (T:9731 annXOStreamStop)", () => {
  test("idempotent — both `ended` and an explicit teardown reach it", async () => {
    const t = track();
    nextStream = () => stream(t);
    await getStream();
    stopStream();
    stopStream();
    expect(t.stops).toBe(1);
    expect(currentStream()).toBeNull();
  });

  test("every track is stopped even when one `stop()` throws", async () => {
    // A track that has already ended throws on stop in some engines; the stream
    // is not left half-released because of it.
    const bad = track("live", true);
    const good = track();
    nextStream = () => stream(bad, good);
    await getStream();
    expect(() => stopStream()).not.toThrow();
    expect([bad.stops, good.stops]).toEqual([1, 1]);
  });
});

describe("watchStreamTeardown (T:9731 annXOStreamStop)", () => {
  test("`pagehide` ends the share, and the teardown both unregisters and ends it", async () => {
    // A kept stream IS the browser's "sharing this tab" indicator, so one that
    // survives leaving the chat is the page claiming to watch a screen nobody is
    // looking at any more.
    const added: unknown[][] = [];
    const removed: unknown[][] = [];
    const win = {
      addEventListener: (...a: unknown[]) => added.push(a),
      removeEventListener: (...a: unknown[]) => removed.push(a),
    } as unknown as Window;

    const stop = watchStreamTeardown(win);
    expect(added.length).toBe(1);
    expect(added[0][0]).toBe("pagehide");

    const t = track();
    nextStream = () => stream(t);
    await getStream();
    (added[0][1] as () => void)();
    expect(currentStream()).toBeNull();
    expect(t.stops).toBe(1);

    const t2 = track();
    nextStream = () => stream(t2);
    await getStream();
    stop();
    // The SAME function object, or the removal is a no-op that leaves the
    // listener behind for the life of the page.
    expect(removed[0][0]).toBe("pagehide");
    expect(removed[0][1]).toBe(added[0][1]);
    expect(currentStream()).toBeNull();
    expect(t2.stops).toBe(1);
  });

  test("no window still hands back a teardown that releases the share", async () => {
    const t = track();
    nextStream = () => stream(t);
    await getStream();
    watchStreamTeardown(null)();
    expect(currentStream()).toBeNull();
    expect(t.stops).toBe(1);
    expect(() => watchStreamTeardown(undefined)()).not.toThrow();
    expect(() => watchStreamTeardown({} as unknown as Window)()).not.toThrow();
  });
});

// ── captureXO ───────────────────────────────────────────────────────────────

describe("captureXO the cannot roads answer null (T:9735)", () => {
  test("no frame", async () => {
    expect(await captureXO(null)).toBeNull();
    expect(prompts.length).toBe(0);
  });

  test("no mediaDevices at all, and mediaDevices with no getDisplayMedia", async () => {
    // An older or a non-secure context: never a throw, because the caller falls
    // through to the DOM clone.
    installNavigator({});
    expect(await captureXO(makeFrame(rect(0, 0, 400, 300)))).toBeNull();
    installNavigator({ mediaDevices: {} });
    expect(await captureXO(makeFrame(rect(0, 0, 400, 300)))).toBeNull();
  });

  test("a host window that throws on read is null before any prompt is raised", async () => {
    // The crop needs the host's innerWidth; without it there is no arithmetic to
    // do, so the user is not asked to share a tab we could not have cropped.
    const bad = {
      get innerWidth(): number {
        throw new Error("cross-origin");
      },
    } as unknown as Window;
    expect(await captureXO(makeFrame(rect(0, 0, 400, 300)), bad)).toBeNull();
    expect(prompts.length).toBe(0);
  });

  test("a frame whose ownerDocument throws is null", async () => {
    const frame: Rec = { getBoundingClientRect: () => rect(0, 0, 400, 300) };
    Object.defineProperty(frame, "ownerDocument", {
      get() {
        throw new Error("cross-origin");
      },
    });
    expect(await captureXO(frame as unknown as HTMLIFrameElement)).toBeNull();
    expect(prompts.length).toBe(0);
  });
});

describe("captureXO the crop arithmetic (T:9748, 9752)", () => {
  test("videoWidth/innerWidth scales the frame's rect into drawImage's SOURCE box", async () => {
    // The captured frame is the tab's viewport at the capture's own resolution;
    // the iframe's rect in that viewport, scaled the same way, is the crop. Here
    // 2000/1000 and 1200/600 make the scale exactly 2 in both axes.
    const out = await captureXO(makeFrame(rect(100, 50, 300.5, 200.25)));
    const video = videos[0];
    expect(drawn.length).toBe(1);
    expect(drawn[0][0]).toBe(video);
    expect(drawn[0].slice(1)).toEqual([200, 100, 601, 400.5, 0, 0, 301, 200]);
    // The OUTPUT canvas is the frame's CSS size, not the capture's resolution:
    // every pin and crop rect downstream is in the framed viewport's CSS pixels.
    expect([canvases[0].width, canvases[0].height]).toEqual([301, 200]);
    expect([out?.width, out?.height]).toEqual([301, 200]);
    // A capture off live pixels has no style walk, no image inlining and no WebGL
    // readback to caveat, so the doubt fields are simply clean.
    expect(out?.incomplete).toBe(false);
    expect(out?.blanks).toEqual([]);
    expect(out?.styled).toBe(0);
    expect(out?.imagesMissing).toBe(0);
    // Muted, and let go of afterwards — a held srcObject keeps decoding a stream
    // nobody is drawing.
    expect(video.muted).toBe(true);
    expect(video.srcObject).toBeNull();
  });

  test("an explicit host window wins over the frame's own, and rescales the crop", async () => {
    // The overlay's host is the window the rect was measured in; a different
    // innerWidth is a different scale for the same rect.
    await captureXO(makeFrame(rect(100, 50, 300, 200)), hostWindow(2000, 1200));
    expect(drawn[0].slice(1)).toEqual([100, 50, 300, 200, 0, 0, 300, 200]);
  });

  test("a degenerate rect still yields a 1x1 canvas rather than a 0-sized one", async () => {
    // `getContext` on a 0-width canvas is a hard failure in real browsers, so the
    // floor is not cosmetic.
    const out = await captureXO(makeFrame(rect(0, 0, 0, 0)));
    expect([out?.width, out?.height]).toEqual([1, 1]);
  });

  test("no requestVideoFrameCallback falls back to a timed wait, and still crops", async () => {
    // `play()` resolving does not mean pixels arrived; where the frame callback
    // does not exist the wait is a plain timer.
    hasVfc = false;
    const out = await captureXO(makeFrame(rect(100, 50, 300, 200)));
    expect(videos[0].requestVideoFrameCallback).toBeUndefined();
    expect(drawn[0].slice(1)).toEqual([200, 100, 600, 400, 0, 0, 300, 200]);
    expect(out).not.toBeNull();
  });

  test("the second capture reuses the share: one prompt, two crops", async () => {
    await captureXO(makeFrame(rect(0, 0, 400, 300)));
    await captureXO(makeFrame(rect(0, 0, 400, 300)));
    expect(prompts.length).toBe(1);
    expect(drawn.length).toBe(2);
    expect(videos[0].srcObject).toBeNull();
  });
});
