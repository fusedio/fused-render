// THE NATIVE SHOT'S DECISION POINTS (T:9766-9956). `capture.test.ts` injects
// `opts.strategies`, so the real native strategy never runs there: what is under
// test HERE is the arithmetic that turns a frame into a screen rect, the origin
// it is learned from, and the "cannot" roads — every one of which has to answer
// null rather than throw, because the caller falls through to the paths that
// were there before. Nothing about pixels: the hand-rolled fakes below carry
// only the members the module actually reads.
import { afterAll, beforeEach, describe, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

// `bun test` runs every file in ONE process against one `globalThis`, so
// whatever this suite stubs is put back the moment it is done — a leaked
// `fetch`, `document` or `window` breaks whoever runs next (the idiom
// platform/ui/appdoctor-lib.test.ts sets out and capture.test.ts follows).
const G = globalThis as Record<string, unknown>;
const BEFORE = {
  fetch: G.fetch,
  createImageBitmap: G.createImageBitmap,
  document: G.document,
  window: G.window,
};
afterAll(() => {
  G.fetch = BEFORE.fetch;
  G.createImageBitmap = BEFORE.createImageBitmap;
  G.document = BEFORE.document;
  G.window = BEFORE.window;
});

const {
  SHOOTING_ATTR,
  captureNative,
  frameOffset,
  isNativeOff,
  learnTopOrigin,
  resetNativeOffForTests,
  resetTopOriginForTests,
  screenRect,
  topOriginForTests,
  watchTopOrigin,
} = await import("./native-capture");
const { SHOT_NATIVE_MIN } = await import("./types");

// ── the fakes ───────────────────────────────────────────────────────────────

type Rec = Record<string, unknown>;

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

/** A frame ELEMENT as `frameOffset` reads one: a client rect plus the border
 *  widths it adds on top. */
function frameEl(r: DOMRect, clientLeft = 0, clientTop = 0): Rec {
  return { getBoundingClientRect: () => r, clientLeft, clientTop };
}

/** A top-level window: `w.top === w`, so `frameOffset`'s walk never runs. The
 *  screen numbers are the fallback origin's inputs — chrome 0px at the sides and
 *  60px on top, which is what makes the fallback's guess (40, 150) below. */
function topWin(over: Rec = {}): Window {
  const w: Rec = {
    innerWidth: 1200,
    innerHeight: 800,
    screenX: 40,
    screenY: 90,
    outerWidth: 1200,
    outerHeight: 860,
    ...over,
  };
  w.top = w;
  w.parent = w;
  return w as unknown as Window;
}

/** A framed window one level below `parent`. */
function childWin(parent: Window, fe: unknown, top?: Window): Window {
  return {
    frameElement: fe,
    parent,
    top: top || (parent as unknown as Rec).top,
  } as unknown as Window;
}

interface FakeCanvas {
  width: number;
  height: number;
  getContext: () => { drawImage: (...args: unknown[]) => void };
}

let drawn: unknown[][] = [];
let canvases: FakeCanvas[] = [];
let flashEls: Rec[] = [];

function newCanvas(): FakeCanvas {
  const c: FakeCanvas = {
    width: 0,
    height: 0,
    getContext: () => ({ drawImage: (...args: unknown[]) => drawn.push(args) }),
  };
  canvases.push(c);
  return c;
}

/** The frame's OWNER document — a distinct object from the global `document` on
 *  purpose, because `flashOverlays` collects both into a Set and a single
 *  shared object would silently test only one of them. */
function ownerDoc(win: Window | null, body: Rec | null): Rec {
  return {
    defaultView: win,
    body,
    querySelectorAll: () => flashEls,
  };
}

interface FrameOver {
  isConnected?: boolean;
  clientWidth?: number;
  clientHeight?: number;
  clientLeft?: number;
  clientTop?: number;
  r?: DOMRect;
  win?: Window | null;
  body?: Rec | null;
  doc?: Rec;
}

function makeFrame(over: FrameOver = {}): HTMLIFrameElement {
  const win = over.win === undefined ? topWin() : over.win;
  const body: Rec | null =
    over.body === undefined ? { attrs: {} as Rec, setAttribute() {}, removeAttribute() {} } : over.body;
  return {
    isConnected: over.isConnected !== false,
    clientWidth: over.clientWidth ?? 400,
    clientHeight: over.clientHeight ?? 300,
    clientLeft: over.clientLeft ?? 3,
    clientTop: over.clientTop ?? 4,
    getBoundingClientRect: () => over.r || rect(100, 50, 400, 300),
    ownerDocument: over.doc || ownerDoc(win, body),
  } as unknown as HTMLIFrameElement;
}

/** A body that REMEMBERS the attribute round trip, so the shot can be asked
 *  whether the shell's overlay chrome was hidden while the screen was read. */
function recordingBody(): Rec & { attrs: Record<string, string> } {
  const attrs: Record<string, string> = {};
  return {
    attrs,
    setAttribute: (k: string, v: string) => {
      attrs[k] = v;
    },
    removeAttribute: (k: string) => {
      delete attrs[k];
    },
  } as Rec & { attrs: Record<string, string> };
}

interface FetchCall {
  url: string;
  body: { rect: number[]; dpr: number };
}
let fetches: FetchCall[] = [];
let respond: () => Promise<unknown> = () => Promise.reject(new Error("no responder"));

beforeEach(() => {
  // The module-level latches are exactly what these tests are about, so neither
  // may leak into the next test — a sticky `nativeOff` would make every later
  // capture answer null before it asked anything.
  resetNativeOffForTests();
  resetTopOriginForTests();
  drawn = [];
  canvases = [];
  flashEls = [];
  fetches = [];
  respond = () => Promise.reject(new Error("no responder"));
  G.document = {
    hidden: false,
    addEventListener() {},
    removeEventListener() {},
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (tag: string) => (tag === "canvas" ? newCanvas() : {}),
  };
  G.window = { ...(BEFORE.window as Rec), devicePixelRatio: 2 };
  G.createImageBitmap = () => Promise.resolve({ close: () => {} });
  G.fetch = (url: string, init: { body: string }) => {
    fetches.push({ url, body: JSON.parse(init.body) as FetchCall["body"] });
    return respond();
  };
});

/** A server answer with only the members `captureNative` reads off a Response. */
function reply(status: number, over: Rec = {}): unknown {
  return {
    status,
    ok: status >= 200 && status < 300,
    json: () => Promise.resolve({ _error: "why" }),
    blob: () => Promise.resolve(new Blob([new Uint8Array(8)], { type: "image/png" })),
    ...over,
  };
}

// ── frameOffset ─────────────────────────────────────────────────────────────

describe("frameOffset (T:9822)", () => {
  test("a top-level window is at the top viewport's own origin", () => {
    expect(frameOffset(topWin())).toEqual({ x: 0, y: 0 });
  });

  test("every frame element between here and the top is summed, borders included", () => {
    // Two levels, so the test would still fail if the walk stopped after one.
    // The border widths are part of the sum because the offset has to land on the
    // framed VIEWPORT, not on the element box around it.
    const top = topWin();
    const mid = childWin(top, frameEl(rect(5, 7, 900, 600)));
    const leaf = childWin(mid, frameEl(rect(10, 20, 400, 300), 1, 2), top);
    expect(frameOffset(leaf)).toEqual({ x: 16, y: 29 });
  });

  test("a frame on the way with no element is null, not a partial sum", () => {
    // Half an offset would be a confidently WRONG screen rect; null makes the
    // caller fall through instead.
    const top = topWin();
    expect(frameOffset(childWin(top, undefined))).toBeNull();
  });

  test("a cross-origin ancestor that throws on read is null", () => {
    const top = topWin();
    const leaf: Rec = { parent: top, top };
    Object.defineProperty(leaf, "frameElement", {
      get() {
        throw new Error("cross-origin");
      },
    });
    expect(frameOffset(leaf as unknown as Window)).toBeNull();
  });
});

// ── learnTopOrigin / watchTopOrigin ─────────────────────────────────────────

describe("learnTopOrigin (T:9802, 9812)", () => {
  test("screen minus client IS the viewport origin, minus our own frame offset", () => {
    // The whole point of learning from a pointer event: screenX/clientX is exact
    // for any chrome layout, where the outerWidth arithmetic only guesses.
    learnTopOrigin({ screenX: 500, clientX: 100, screenY: 300, clientY: 50 }, topWin());
    expect(topOriginForTests()).toEqual({ x: 400, y: 250 });
  });

  test("a click inside a frame is walked up to the TOP window's origin", () => {
    const top = topWin();
    const mid = childWin(top, frameEl(rect(5, 7, 900, 600)));
    const leaf = childWin(mid, frameEl(rect(10, 20, 400, 300), 1, 2), top);
    learnTopOrigin({ screenX: 500, clientX: 100, screenY: 300, clientY: 50 }, leaf);
    // (500-100) - 16, (300-50) - 29.
    expect(topOriginForTests()).toEqual({ x: 384, y: 221 });
  });

  test("an unknowable frame offset teaches NOTHING rather than a wrong origin", () => {
    learnTopOrigin({ screenX: 500, clientX: 100, screenY: 300, clientY: 50 }, childWin(topWin(), undefined));
    expect(topOriginForTests()).toBeNull();
  });
});

describe("watchTopOrigin (T:9802)", () => {
  test("the listener is CAPTURE phase, learns on a pointerdown, and the teardown removes it", () => {
    // Capture phase so a `stopPropagation` in a menu cannot hide the click the
    // next capture depends on — and registered from here rather than at module
    // load, so a bundle that merely imports `shots/*` carries no listener.
    const added: unknown[][] = [];
    const removed: unknown[][] = [];
    const host = topWin({
      addEventListener: (...a: unknown[]) => added.push(a),
      removeEventListener: (...a: unknown[]) => removed.push(a),
    });
    const stop = watchTopOrigin(host);
    expect(added.length).toBe(1);
    expect(added[0][0]).toBe("pointerdown");
    expect(added[0][2]).toEqual({ capture: true, passive: true });

    const onDown = added[0][1] as (e: unknown) => void;
    onDown({ screenX: 500, clientX: 100, screenY: 300, clientY: 50 });
    expect(topOriginForTests()).toEqual({ x: 400, y: 250 });

    stop();
    expect(removed.length).toBe(1);
    expect(removed[0][0]).toBe("pointerdown");
    // The SAME function object, or the removal is a no-op that leaves the
    // listener behind for the life of the page.
    expect(removed[0][1]).toBe(onDown);
    expect(removed[0][2]).toEqual({ capture: true });
  });

  test("no window, or one that cannot listen, is a harmless no-op teardown", () => {
    expect(() => watchTopOrigin(null)()).not.toThrow();
    expect(() => watchTopOrigin(undefined)()).not.toThrow();
    expect(() => watchTopOrigin({} as unknown as Window)()).not.toThrow();
  });
});

// ── screenRect ──────────────────────────────────────────────────────────────

describe("screenRect (T:9838)", () => {
  test("the border is trimmed and the fallback origin is added", () => {
    // left = frameOffset.x + rect.left + clientLeft = 0 + 100 + 3, and the origin
    // guess is screenX + 0 chrome at the sides, screenY + 60px on top.
    expect(screenRect(makeFrame())).toEqual({
      rect: [40 + 103, 150 + 54, 400, 300],
      width: 400,
      height: 300,
    });
  });

  test("a LEARNED origin beats the outerWidth guess", () => {
    learnTopOrigin({ screenX: 500, clientX: 100, screenY: 300, clientY: 50 }, topWin());
    const box = screenRect(makeFrame());
    expect(box?.rect).toEqual([400 + 103, 250 + 54, 400, 300]);
  });

  test("the frame's CLIENT size wins over its client rect", () => {
    // The rect includes the border; the client size is the framed viewport, which
    // is the space every pin and crop rect downstream already lives in.
    const box = screenRect(makeFrame({ clientWidth: 380, clientHeight: 280 }));
    expect([box?.width, box?.height]).toEqual([380, 280]);
  });

  test("a frame in a document with no window is null", () => {
    expect(screenRect(makeFrame({ win: null }))).toBeNull();
  });

  test("an unknowable frame offset is null", () => {
    const top = topWin();
    expect(screenRect(makeFrame({ win: childWin(top, undefined) }))).toBeNull();
  });

  test("too small to be worth photographing is null", () => {
    expect(
      screenRect(makeFrame({ clientWidth: SHOT_NATIVE_MIN.width - 1, clientHeight: 300 })),
    ).toBeNull();
    expect(
      screenRect(makeFrame({ clientWidth: 400, clientHeight: SHOT_NATIVE_MIN.height - 1 })),
    ).toBeNull();
    // And the boundary itself is IN: the constant is a minimum, not a threshold
    // one pixel above it.
    expect(
      screenRect(
        makeFrame({
          clientWidth: SHOT_NATIVE_MIN.width,
          clientHeight: SHOT_NATIVE_MIN.height,
          r: rect(0, 0, SHOT_NATIVE_MIN.width, SHOT_NATIVE_MIN.height),
        }),
      ),
    ).not.toBeNull();
  });

  test("off screen — scrolled above or left of the viewport — is null", () => {
    expect(screenRect(makeFrame({ r: rect(-200, 50, 400, 300) }))).toBeNull();
    expect(screenRect(makeFrame({ r: rect(100, -100, 400, 300) }))).toBeNull();
  });

  test("a SLIVER hanging out of the top viewport is null, not a wrong picture", () => {
    // 1000 + 3 + 400 runs past innerWidth 1200: the server would refuse the rect
    // anyway, and a partial frame is a picture of the wrong thing.
    expect(screenRect(makeFrame({ r: rect(1000, 50, 400, 300) }))).toBeNull();
    expect(screenRect(makeFrame({ r: rect(100, 700, 400, 300) }))).toBeNull();
  });

  test("a top window we cannot read is null — no origin, no rect", () => {
    const badTop: Rec = {
      get innerWidth(): number {
        throw new Error("cross-origin");
      },
    };
    badTop.top = badTop;
    const win = childWin(badTop as unknown as Window, frameEl(rect(0, 0, 900, 600)));
    expect(screenRect(makeFrame({ win }))).toBeNull();
  });
});

// ── captureNative ───────────────────────────────────────────────────────────

describe("captureNative the 409 sticky latch (T:9790, 9920, 9924)", () => {
  test("a 409 is REMEMBERED: the second capture makes no request at all", async () => {
    // 409 means "this platform has no still" — a fact about the backend, not
    // about this shot, so a page over an unsupported backend pays the round trip
    // exactly once instead of on every click.
    const warn = console.warn;
    console.warn = () => {};
    try {
      respond = () => Promise.resolve(reply(409));
      expect(isNativeOff()).toBe(false);
      expect(await captureNative(makeFrame())).toBeNull();
      expect(fetches.length).toBe(1);
      expect(isNativeOff()).toBe(true);

      expect(await captureNative(makeFrame())).toBeNull();
      expect(fetches.length).toBe(1);
    } finally {
      console.warn = warn;
    }
  });

  test("a 400 is about THIS shot only — the next one still asks", async () => {
    const warned: unknown[] = [];
    const warn = console.warn;
    console.warn = (...a: unknown[]) => warned.push(a[0]);
    try {
      respond = () => Promise.resolve(reply(400));
      expect(await captureNative(makeFrame())).toBeNull();
      expect(isNativeOff()).toBe(false);
      expect(await captureNative(makeFrame())).toBeNull();
      expect(fetches.length).toBe(2);
      // Said, not swallowed: a 400 on every click is a bug in the arithmetic
      // above that only the console can show.
      expect(warned[0]).toBe("native pane shot refused (400): why");
    } finally {
      console.warn = warn;
    }
  });
});

describe("captureNative the cannot roads answer null (T:9905)", () => {
  test("no frame, and a frame no longer in the document", async () => {
    expect(await captureNative(null)).toBeNull();
    expect(await captureNative(makeFrame({ isConnected: false }))).toBeNull();
    // Neither reached the network: there is nothing to photograph.
    expect(fetches.length).toBe(0);
  });

  test("an uncomputable origin never asks the server", async () => {
    expect(await captureNative(makeFrame({ win: childWin(topWin(), undefined) }))).toBeNull();
    expect(fetches.length).toBe(0);
  });

  test("a hidden pane — too small to photograph — never asks the server", async () => {
    expect(await captureNative(makeFrame({ clientWidth: 0, clientHeight: 0, r: rect(0, 0, 0, 0) }))).toBeNull();
    expect(fetches.length).toBe(0);
  });

  test("an unreachable server is null, not a rejection", async () => {
    respond = () => Promise.reject(new Error("ECONNREFUSED"));
    expect(await captureNative(makeFrame())).toBeNull();
    expect(fetches.length).toBe(1);
    expect(isNativeOff()).toBe(false);
  });

  test("bytes that are not a PNG are null", async () => {
    respond = () =>
      Promise.resolve(reply(200, { blob: () => Promise.resolve(new Blob([], { type: "text/html" })) }));
    expect(await captureNative(makeFrame())).toBeNull();
  });
});

describe("captureNative the success road (T:9878, 9884, 9944)", () => {
  test("the rect, the dpr, and the overlay/SHOOTING_ATTR round trip", async () => {
    const body = recordingBody();
    const flash: Rec = { style: { visibility: "visible" } };
    flashEls = [flash];
    // A no-`style` member of the default overlay set is filtered out rather than
    // throwing on assignment.
    const notAnElement: Rec = {};
    flashEls.push(notAnElement);

    let duringAttr: string | undefined;
    let duringVis: unknown;
    respond = () => {
      duringAttr = body.attrs[SHOOTING_ATTR];
      duringVis = (flash.style as Rec).visibility;
      return Promise.resolve(reply(200));
    };

    const frame = makeFrame({ body });
    const out = await captureNative(frame, Date.now() + 1000);

    expect(fetches[0].url).toBe("/api/capture/shot-region");
    expect(fetches[0].body).toEqual({ rect: [143, 204, 400, 300], dpr: 2 });
    // The screen shot sees everything the user does, so the shell's own chrome
    // and the flash sheet are hidden WHILE the pixels are read…
    expect(duringAttr).toBe("");
    expect(duringVis).toBe("hidden");
    // …and put back afterwards, whatever happened in between.
    expect(SHOOTING_ATTR in body.attrs).toBe(false);
    expect((flash.style as Rec).visibility).toBe("visible");

    // Drawn at the frame's CSS size, not the display's: every crop rect and badge
    // position downstream is in the framed viewport's CSS pixels.
    expect(out?.width).toBe(400);
    expect(out?.height).toBe(300);
    expect([canvases[0].width, canvases[0].height]).toEqual([400, 300]);
    expect(drawn[0].slice(1)).toEqual([0, 0, 400, 300]);
    // A live-pixel capture has no style walk and no image inlining to caveat, so
    // the doubt fields are simply clean.
    expect(out?.incomplete).toBe(false);
    expect(out?.blanks).toEqual([]);
    expect(out?.imagesMissing).toBe(0);
  });

  test("the overlays are RESTORED even when the request fails", async () => {
    const body = recordingBody();
    const flash: Rec = { style: { visibility: "inherit" } };
    respond = () => Promise.reject(new Error("boom"));
    expect(await captureNative(makeFrame({ body }), undefined, { overlays: () => [flash as unknown as Element] })).toBeNull();
    expect((flash.style as Rec).visibility).toBe("inherit");
    expect(SHOOTING_ATTR in body.attrs).toBe(false);
  });

  test("an injected overlay set replaces the default flash lookup", async () => {
    // PR3 passes its own set, which adds the annotation shadow host.
    flashEls = [{ style: { visibility: "visible" } }];
    const mine: Rec = { style: { visibility: "visible" } };
    let duringFlash: unknown;
    let duringMine: unknown;
    respond = () => {
      duringFlash = (flashEls[0].style as Rec).visibility;
      duringMine = (mine.style as Rec).visibility;
      return Promise.resolve(reply(200));
    };
    await captureNative(makeFrame(), undefined, { overlays: () => [mine as unknown as Element] });
    expect(duringMine).toBe("hidden");
    expect(duringFlash).toBe("visible");
  });
});
