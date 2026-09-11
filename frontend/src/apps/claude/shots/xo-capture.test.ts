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

const {
  captureXO,
  currentStream,
  getStream,
  releaseXOTarget,
  stopStream,
  streamCaptures,
  streamReleasePending,
  streamWatchers,
  watchStreamTeardown,
} = await import("./xo-capture");

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

/** Called at the instant `drawImage` reads the video — the only moment at which
 *  "was the flash on screen?" has an answer. */
let onDraw: (() => void) | null = null;

function newCanvas(): FakeCanvas {
  const c: FakeCanvas = {
    width: 0,
    height: 0,
    getContext: () => ({
      drawImage: (...args: unknown[]) => {
        drawn.push(args);
        if (onDraw) onDraw();
      },
    }),
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
  onDraw = null;
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

  test("REFERENCE-COUNTED: one chat closing does not revoke another's share", async () => {
    // The stream is a module singleton and EVERY `ChatBody` registers a
    // teardown, so a teardown that always stopped meant a card or peek
    // unmounting killed the share the chat next to it was mid-walkthrough with
    // (Bugbot, PR #1064).
    const listeners: Record<string, unknown[]> = { added: [], removed: [] };
    const win = {
      addEventListener: (...a: unknown[]) => listeners.added.push(a),
      removeEventListener: (...a: unknown[]) => listeners.removed.push(a),
    } as unknown as Window;

    const first = watchStreamTeardown(win);
    const second = watchStreamTeardown(win);
    expect(streamWatchers()).toBe(2);

    const t = track();
    nextStream = () => stream(t);
    await getStream();

    // The first one out unregisters ITS listener and leaves the share alone.
    first();
    expect(listeners.removed.length).toBe(1);
    expect(currentStream()).not.toBeNull();
    expect(t.stops).toBe(0);
    // Twice is the same as once: a double-invoked teardown must not decrement a
    // registration it already gave back.
    first();
    expect(streamWatchers()).toBe(1);
    expect(currentStream()).not.toBeNull();

    // The LAST one out ends it.
    second();
    expect(streamWatchers()).toBe(0);
    expect(currentStream()).toBeNull();
    expect(t.stops).toBe(1);
  });

  test("`pagehide` ends the share outright, watchers or not", async () => {
    // There is no page left to share to, so the count does not get a vote.
    const hides: (() => void)[] = [];
    const win = {
      addEventListener: (_k: unknown, fn: () => void) => hides.push(fn),
      removeEventListener: () => {},
    } as unknown as Window;
    const a = watchStreamTeardown(win);
    const b = watchStreamTeardown(win);
    const t = track();
    nextStream = () => stream(t);
    await getStream();
    hides[0]!();
    expect(currentStream()).toBeNull();
    expect(t.stops).toBe(1);
    a();
    b();
    expect(streamWatchers()).toBe(0);
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

// ── the shutter flash (Bugbot, PR #1064) ────────────────────────────────────
//
// This road photographs the SCREEN, so it sees everything the user does — the
// white sheet `flash()` puts over the pane included. The native path always hid
// it; here, with a KEPT share, there is no picker to sit through and the grab
// beat the flash's 340 ms home, so the sheet was burned into the picture.

describe("captureXO hides what sits ON the pane", () => {
  function flashSheet(visibility = ""): { style: { visibility: string } } {
    const el = { style: { visibility } };
    const doc = G.document as Rec;
    doc.querySelectorAll = (sel: string) => (sel === "[data-shot-flash]" ? [el] : []);
    return el;
  }

  test("the sheet is hidden for the grab and put back exactly as it was", async () => {
    const el = flashSheet("visible");
    let during = "?";
    onDraw = () => {
      during = el.style.visibility;
    };
    const out = await captureXO(makeFrame(rect(0, 0, 400, 300)));
    expect(out).not.toBeNull();
    expect(during).toBe("hidden");
    // PUT BACK, and to its own prior value rather than to "": a page left with
    // an invisible overlay is worse than a flash in one picture.
    expect(el.style.visibility).toBe("visible");
  });

  test("a hide waits for a FRESH frame — the one in flight was composited before it", async () => {
    // A video element hands over the last frame it decoded, and that frame was
    // painted while the sheet was still up.
    const el = flashSheet();
    const frames: string[] = [];
    const doc = G.document as Rec;
    const make = doc.createElement as (tag: string) => FakeVideo | FakeCanvas;
    doc.createElement = (tag: string) => {
      const node = make(tag);
      const v = node as FakeVideo;
      if (v.requestVideoFrameCallback) {
        v.requestVideoFrameCallback = (cb: () => void) => {
          frames.push(el.style.visibility);
          cb();
        };
      }
      return node;
    };
    await captureXO(makeFrame(rect(0, 0, 400, 300)));
    expect(frames).toEqual(["hidden", "hidden"]);
  });

  test("nothing to hide is one frame's wait, unchanged", async () => {
    // The overlay is only up for a click that flashed; the common capture pays
    // nothing for this.
    const frames: number[] = [];
    const doc = G.document as Rec;
    const make = doc.createElement as (tag: string) => FakeVideo | FakeCanvas;
    doc.createElement = (tag: string) => {
      const node = make(tag);
      const v = node as FakeVideo;
      if (v.requestVideoFrameCallback) {
        v.requestVideoFrameCallback = (cb: () => void) => {
          frames.push(1);
          cb();
        };
      }
      return node;
    };
    await captureXO(makeFrame(rect(0, 0, 400, 300)));
    expect(frames.length).toBe(1);
  });

  test("the sheet comes back even when the grab throws", async () => {
    const el = flashSheet("visible");
    const doc = G.document as Rec;
    const make = doc.createElement as (tag: string) => FakeVideo | FakeCanvas;
    doc.createElement = (tag: string) =>
      tag === "canvas"
        ? ({
            width: 0,
            height: 0,
            getContext: () => {
              throw new Error("context lost");
            },
          } as unknown as FakeCanvas)
        : make(tag);
    await expect(captureXO(makeFrame(rect(0, 0, 400, 300)))).rejects.toThrow("context lost");
    expect(el.style.visibility).toBe("visible");
  });

  test("an overlay with no style, and a document that refuses to be queried", async () => {
    // A finder that throws would take the whole capture with it rather than
    // simply hiding nothing.
    const doc = G.document as Rec;
    doc.querySelectorAll = () => {
      throw new Error("not queryable");
    };
    expect(await captureXO(makeFrame(rect(0, 0, 400, 300)))).not.toBeNull();
    doc.querySelectorAll = () => [{} as unknown as Element];
    expect(await captureXO(makeFrame(rect(0, 0, 400, 300)))).not.toBeNull();
  });
});

// ── the bound on a frame that never arrives (D5) ────────────────────────────

describe("nextFrame is BOUNDED even where rVFC exists (D5)", () => {
  test("a video whose frame callback never fires still settles the capture", async () => {
    // `requestVideoFrameCallback` fires off the compositor: a hidden document (a
    // cmux pane, a background tab) decodes no frames and the callback never
    // comes. Unbounded, the whole capture hung — so the `finally` never ran, the
    // flash overlay stayed invisible and the tab share was never released.
    const el = { style: { visibility: "visible" } };
    const doc = G.document as Rec;
    doc.querySelectorAll = (sel: string) => (sel === "[data-shot-flash]" ? [el] : []);
    const make = doc.createElement as (tag: string) => FakeVideo | FakeCanvas;
    doc.createElement = (tag: string) => {
      const node = make(tag);
      const v = node as FakeVideo;
      if (v.requestVideoFrameCallback) v.requestVideoFrameCallback = () => {};
      return node;
    };
    const started = Date.now();
    const out = await captureXO(makeFrame(rect(0, 0, 400, 300)));
    // A stale frame is a far better answer than a hang: the crop is drawn and
    // the overlay is put back, inside the bound (two waits, because a hide
    // happened) rather than never.
    expect(out).not.toBeNull();
    expect(drawn.length).toBe(1);
    expect(el.style.visibility).toBe("visible");
    expect(Date.now() - started).toBeLessThan(1000);
  });
});

// ── one share, however many askers (D6) ─────────────────────────────────────

describe("getStream is SINGLE-FLIGHT (D6)", () => {
  /** A prompt the test resolves by hand — the only way to have two callers
   *  inside `getDisplayMedia` at once, which is where the double share came
   *  from. */
  function parkedPrompt(): (s: FakeStream) => void {
    let release: (s: FakeStream) => void = () => {};
    G.navigator = {
      mediaDevices: {
        getDisplayMedia: (c: Rec) => {
          prompts.push(c);
          return new Promise<FakeStream>((res) => {
            release = res;
          });
        },
      },
    } as Rec;
    return (s) => release(s);
  }

  test("two callers in the same tick share ONE prompt and one stream", async () => {
    // `stream` is only assigned when the picker comes back, so both callers saw
    // "nothing held", both prompted, and the second share overwrote the first —
    // which nothing then held or released.
    const release = parkedPrompt();
    const a = getStream();
    const b = getStream();
    expect(prompts.length).toBe(1);
    const s = stream(track());
    release(s);
    expect(await a).toBe(await b);
    expect(prompts.length).toBe(1);
  });

  test("two concurrent captures open one share, not two", async () => {
    const release = parkedPrompt();
    const first = captureXO(makeFrame(rect(0, 0, 400, 300)));
    const second = captureXO(makeFrame(rect(0, 0, 400, 300)));
    expect(prompts.length).toBe(1);
    release(stream(track()));
    expect(await first).not.toBeNull();
    expect(await second).not.toBeNull();
    expect(prompts.length).toBe(1);
  });

  test("a stop while the prompt is out is not handed the stale share", async () => {
    // A share asked for BEFORE a `stopStream` must not be given to callers after
    // it as if it were still held.
    const release = parkedPrompt();
    const asked = getStream();
    stopStream();
    const orphan = track();
    release(stream(orphan));
    await asked;
    // And the orphan is ENDED rather than left running: an installed-too-late
    // share is the "sharing this tab" indicator with no chat behind it.
    expect(orphan.stops).toBe(1);
    expect(currentStream()).toBeNull();
    const fresh = parkedPrompt();
    const again = getStream();
    expect(prompts.length).toBe(2);
    fresh(stream(track()));
    await again;
  });
});

describe("an in-flight capture HOLDS the share (D6)", () => {
  function parkedPrompt(): (s: FakeStream) => void {
    let release: (s: FakeStream) => void = () => {};
    G.navigator = {
      mediaDevices: {
        getDisplayMedia: (c: Rec) => {
          prompts.push(c);
          return new Promise<FakeStream>((res) => {
            release = res;
          });
        },
      },
    } as Rec;
    return (s) => release(s);
  }

  test("the chat closing mid-capture defers the release; the capture pays it", async () => {
    // Before this, the unmount stopped the stream under the capture, the capture
    // re-prompted, and THAT share had no watcher left to release it — the
    // browser's "sharing this tab" indicator outlived the closed chat.
    const release = parkedPrompt();
    const leave = watchStreamTeardown(null);
    const shot = captureXO(makeFrame(rect(0, 0, 400, 300)));
    expect(streamCaptures()).toBe(1);

    leave();
    expect(streamWatchers()).toBe(0);
    expect(streamReleasePending()).toBe(true);

    const t = track();
    release(stream(t));
    // The capture still gets its picture...
    expect(await shot).not.toBeNull();
    // ...and the share is handed back the moment it lands, by the last holder out.
    expect(t.stops).toBe(1);
    expect(currentStream()).toBeNull();
    expect(streamCaptures()).toBe(0);
    expect(streamReleasePending()).toBe(false);
  });

  test("with a watcher still mounted the capture ending keeps the share", async () => {
    // Nobody asked for the release, so none is owed: the prompt stays paid once
    // per walkthrough rather than once per note.
    const leave = watchStreamTeardown(null);
    const t = track();
    nextStream = () => stream(t);
    expect(await captureXO(makeFrame(rect(0, 0, 400, 300)))).not.toBeNull();
    expect(currentStream()).not.toBeNull();
    expect(t.stops).toBe(0);
    leave();
    expect(t.stops).toBe(1);
  });
});

// ── the target stops being cross-origin (T:6171-6175) ──────────────────────

describe("releaseXOTarget", () => {
  test("the share goes when the overlay does, even with the chat still mounted", async () => {
    // T hangs `annXOStreamStop()` off `annXORemove()` and says why at the site:
    // "a target that stopped being cross-origin (or went away) has shotPane's
    // own path back, and holding a tab share open past its use is a recording
    // indicator with no purpose." This is NOT the mount leaving — `watchers` is
    // still 1 — which is exactly why the ordinary idle test refused and the
    // "sharing this tab" chip outlived the only thing it was for.
    const t = track();
    nextStream = () => stream(t);
    const leave = watchStreamTeardown(null);
    await getStream();
    expect(currentStream()).not.toBeNull();
    expect(streamWatchers()).toBe(1);

    releaseXOTarget();
    expect(t.stops).toBe(1);
    expect(currentStream()).toBeNull();
    leave();
  });

  test("another mount's XO target keeps it — the last one out does the stopping", async () => {
    const t = track();
    nextStream = () => stream(t);
    const a = watchStreamTeardown(null);
    const b = watchStreamTeardown(null);
    await getStream();

    releaseXOTarget();
    // Two mounts, so this one cannot know the share is not still in use; a
    // mount that really needs it again re-opens through `getStream`'s single
    // flight.
    expect(t.stops).toBe(0);
    expect(currentStream()).not.toBeNull();

    a();
    releaseXOTarget();
    expect(t.stops).toBe(1);
    b();
  });

  test("a capture in flight defers it, and the capture pays it (D6)", async () => {
    // A prompt held open, so the capture is genuinely mid-flight when the
    // overlay goes (the sibling describe's `parkedPrompt`, inlined — it is
    // scoped to that block).
    let settle: (s: FakeStream) => void = () => {};
    G.navigator = {
      mediaDevices: {
        getDisplayMedia: (c: Rec) => {
          prompts.push(c);
          return new Promise<FakeStream>((res) => {
            settle = res;
          });
        },
      },
    } as Rec;
    const release = (s: FakeStream) => settle(s);
    const leave = watchStreamTeardown(null);
    const shot = captureXO(makeFrame(rect(0, 0, 400, 300)));
    expect(streamCaptures()).toBe(1);

    releaseXOTarget();
    // Asked for, not taken: stopping under the capture would make it re-prompt,
    // and that second share would have nobody left to release it.
    expect(streamReleasePending()).toBe(true);

    const t = track();
    release(stream(t));
    expect(await shot).not.toBeNull();
    expect(t.stops).toBe(1);
    expect(currentStream()).toBeNull();
    expect(streamReleasePending()).toBe(false);
    leave();
  });

  test("a target BACK under the capture keeps the new arm's share (finding #3)", async () => {
    // The sticky-flag race. The share is live and a capture is reading it when
    // the target stops being cross-origin: the release is deferred (D6). The
    // reader then re-arms over a NEW cross-origin target, which asks for the
    // share and is handed the live one — so by the time the first capture
    // finishes, the ask belongs to a target that is no longer gone. A flag
    // cleared only by `stopStream` killed that arm's share on the capture's way
    // out, and the re-prompt had no user activation left behind it.
    const t = track();
    nextStream = () => stream(t);
    const leave = watchStreamTeardown(null);
    await getStream();

    const shot = captureXO(makeFrame(rect(0, 0, 400, 300)));
    expect(streamCaptures()).toBe(1);
    releaseXOTarget();
    expect(streamReleasePending()).toBe(true);

    // The re-arm — one acquisition later, so the ask is spent.
    expect(await getStream()).toBe(currentStream() as MediaStream);
    expect(streamReleasePending()).toBe(false);

    expect(await shot).not.toBeNull();
    expect(t.stops).toBe(0);
    expect(currentStream()).not.toBeNull();

    // …and the share is still ordinary: the mount leaving ends it.
    leave();
    expect(t.stops).toBe(1);
  });

  test("no share held: a no-op, not a throw", () => {
    expect(() => releaseXOTarget()).not.toThrow();
    expect(currentStream()).toBeNull();
  });
});
