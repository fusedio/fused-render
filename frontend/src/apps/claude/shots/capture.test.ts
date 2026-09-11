// The capture ORDER and the budget (T:9958, 10199, 11278). The three strategies
// are injected: what is under test is which one is asked, in what order, and what
// the caller gets when none of them answers in time — not the browser's pixels.
import { afterAll, beforeEach, describe, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

// `bun test` runs every file in ONE process and these globals are shared, so
// whatever this suite stubbed is put back the moment it is done — a leaked
// `fetch` or `URL.createObjectURL` breaks whoever runs next (the idiom
// platform/ui/appdoctor-lib.test.ts sets out).
const G = globalThis as Record<string, unknown>;
const BEFORE = {
  fetch: G.fetch,
  createImageBitmap: G.createImageBitmap,
  Image: G.Image,
  createObjectURL: URL.createObjectURL,
  revokeObjectURL: URL.revokeObjectURL,
};
afterAll(() => {
  G.fetch = BEFORE.fetch;
  G.createImageBitmap = BEFORE.createImageBitmap;
  G.Image = BEFORE.Image;
  Object.assign(URL, {
    createObjectURL: BEFORE.createObjectURL,
    revokeObjectURL: BEFORE.revokeObjectURL,
  });
});

const { capturePane, capturePaneBitmap, captureOverview, frameIsCrossOrigin } = await import(
  "./capture"
);
const { resetWebpLatchForTests, setCanvasFactory } = await import("./encode");
const { APP_STATE_UNREADABLE } = await import("../pane/paneUrl");
type PaneBitmap = import("./types").PaneBitmap;

let revoked: string[] = [];
let urls = 0;
let encodes = 0;
/** null = the encoder answers nothing, which is the over-budget path. */
let encoded: Blob | null = new Blob([new Uint8Array(8)], { type: "image/png" });

function installCanvas(): void {
  setCanvasFactory(
    () =>
      ({
        width: 0,
        height: 0,
        getContext: () => ({
          drawImage: () => {},
          save: () => {},
          restore: () => {},
          beginPath: () => {},
          arc: () => {},
          fill: () => {},
          stroke: () => {},
          fillText: () => {},
          fillStyle: "",
          strokeStyle: "",
          lineWidth: 0,
          font: "",
          textAlign: "",
          textBaseline: "",
        }),
        toBlob: (cb: (b: Blob | null) => void) => {
          encodes++;
          cb(encoded);
        },
      }) as unknown as HTMLCanvasElement,
  );
}

function bitmap(over: Partial<PaneBitmap> = {}): PaneBitmap {
  return {
    canvas: {} as CanvasImageSource,
    width: 800,
    height: 600,
    blanks: [],
    styled: 0,
    incomplete: false,
    imagesMissing: 0,
    ...over,
  };
}

const frame = { isConnected: true } as HTMLIFrameElement;

beforeEach(() => {
  revoked = [];
  urls = 0;
  encodes = 0;
  encoded = new Blob([new Uint8Array(8)], { type: "image/png" });
  resetWebpLatchForTests();
  installCanvas();
  Object.assign(URL, {
    createObjectURL: () => "blob:cap/" + ++urls,
    revokeObjectURL: (u: string) => revoked.push(u),
  });
});

describe("capturePaneBitmap order (T:9958)", () => {
  test("native first, and nothing else is asked when it answers", async () => {
    const asked: string[] = [];
    const out = await capturePaneBitmap(frame, Date.now() + 1000, {
      strategies: {
        native: () => (asked.push("native"), Promise.resolve(bitmap())),
        tab: () => (asked.push("tab"), Promise.resolve(bitmap())),
        dom: () => (asked.push("dom"), Promise.resolve(bitmap())),
      },
      appWindow: () => ({}) as Window,
    });
    expect(asked).toEqual(["native"]);
    expect(out.via).toBe("native");
  });

  test("a cross-origin target that native could not shoot takes the TAB share", async () => {
    const asked: string[] = [];
    const out = await capturePaneBitmap(frame, Date.now() + 1000, {
      xo: true,
      strategies: {
        native: () => (asked.push("native"), Promise.resolve(null)),
        tab: () => (asked.push("tab"), Promise.resolve(bitmap())),
        dom: () => (asked.push("dom"), Promise.resolve(bitmap())),
      },
    });
    expect(asked).toEqual(["native", "tab"]);
    expect(out.via).toBe("tab");
  });

  test("A CROSS-ORIGIN FRAME REACHES THE TAB STRATEGY, detected off the frame itself", async () => {
    // The whole point of `frameIsCrossOrigin`: the camera reads it at gesture
    // time and hands it down, and without it `xo` was never true — so a pane
    // this page cannot open fell through to a DOM clone of a document it cannot
    // read, which is no picture at all (Bugbot, PR #1064).
    const xoFrame = {
      isConnected: true,
      get contentDocument(): Document {
        throw new Error("cross-origin");
      },
    } as unknown as HTMLIFrameElement;
    expect(frameIsCrossOrigin(xoFrame)).toBe(true);
    const asked: string[] = [];
    const out = await capturePaneBitmap(xoFrame, Date.now() + 1000, {
      xo: frameIsCrossOrigin(xoFrame),
      strategies: {
        native: () => (asked.push("native"), Promise.resolve(null)),
        tab: () => (asked.push("tab"), Promise.resolve(bitmap())),
        dom: () => (asked.push("dom"), Promise.resolve(bitmap())),
      },
    });
    expect(asked).toEqual(["native", "tab"]);
    expect(out.via).toBe("tab");
  });

  test("frameIsCrossOrigin: a reachable document is ours, no frame is nothing at all", () => {
    // T's `annXO` exactly (T:6123-6129, 6567): a frame is there AND its document
    // is out of reach. No frame is not a cross-origin pane — there is no pane.
    expect(frameIsCrossOrigin(null)).toBe(false);
    expect(
      frameIsCrossOrigin({ contentDocument: {} as Document } as HTMLIFrameElement),
    ).toBe(false);
    // A frame mid-navigation reads the same as a cross-origin one for a beat and
    // resolves on the next gesture: the cost of being wrong this way is one tab
    // prompt, the cost of the other way is a capture that can only fail.
    expect(frameIsCrossOrigin({ contentDocument: null } as HTMLIFrameElement)).toBe(true);
  });

  test("a readable one takes the DOM clone, and never the tab share", async () => {
    const asked: string[] = [];
    const out = await capturePaneBitmap(frame, Date.now() + 1000, {
      strategies: {
        native: () => (asked.push("native"), Promise.resolve(null)),
        tab: () => (asked.push("tab"), Promise.resolve(bitmap())),
        dom: () => (asked.push("dom"), Promise.resolve(bitmap())),
      },
      appWindow: () => ({}) as Window,
    });
    expect(asked).toEqual(["native", "dom"]);
    expect(out.via).toBe("dom");
  });

  test("no readable window and no share is 'none', not a throw", async () => {
    const out = await capturePaneBitmap(frame, Date.now() + 1000, {
      strategies: { native: () => Promise.resolve(null) },
      appWindow: () => null,
    });
    expect(out).toEqual({ pane: null, via: "none" });
  });
});

describe("capturePane", () => {
  test("an unreadable pane answers the sentence, prefixed by the noun (T:10093)", async () => {
    const out = await capturePane(frame, {
      strategies: { native: () => Promise.resolve(null) },
      appWindow: () => null,
    });
    expect(out.blob).toBeNull();
    expect(out.why).toBe("no pane screenshot: " + APP_STATE_UNREADABLE);
  });

  test("over budget names the byte budget (T:10098)", async () => {
    encoded = null;
    const out = await capturePane(frame, {
      strategies: { native: () => Promise.resolve(bitmap()) },
    });
    expect(out.why).toBe("no pane screenshot: it did not fit the 921600-byte budget");
  });

  test("a caveated capture carries its sentences and the trust line (T:10111)", async () => {
    const out = await capturePane(frame, {
      strategies: {
        native: () => Promise.resolve(bitmap({ incomplete: "mutated", styled: 12, imagesMissing: 1 })),
      },
    });
    expect(out.blob).not.toBeNull();
    expect(out.thumb).toBe("blob:cap/1");
    expect(out.notes.length).toBe(2);
    expect(out.incomplete).toBe("mutated");
  });

  test("a THROWN capture is a sentence, never a rejection (T:11297)", async () => {
    const out = await capturePane(frame, {
      strategies: {
        native: () => Promise.reject(new Error("boom")),
      },
    });
    expect(out.why).toBe("no pane screenshot: the capture failed (boom)");
  });

  test("the timeout wins, and the late result's thumbnail is revoked (T:11288)", async () => {
    const out = await capturePane(frame, {
      timeoutMs: 5,
      strategies: {
        native: () =>
          new Promise((res) => setTimeout(() => res(bitmap()), 30)),
      },
    });
    expect(out.blob).toBeNull();
    expect(out.why).toBe("no pane screenshot: the capture did not finish within 5ms and was abandoned");
    // The abandoned capture is watched to the END: it still minted an object URL
    // and the discarded result held the only handle to it.
    await new Promise((res) => setTimeout(res, 60));
    expect(revoked).toEqual(["blob:cap/1"]);
  });

  test("a late REJECTION is warned about, not left unhandled (T:11292)", async () => {
    const warned: unknown[] = [];
    const before = console.warn;
    console.warn = (...args: unknown[]) => warned.push(args[0]);
    try {
      const out = await capturePane(frame, {
        timeoutMs: 5,
        strategies: {
          native: () => new Promise((_res, rej) => setTimeout(() => rej(new Error("late")), 30)),
        },
      });
      expect(out.blob).toBeNull();
      await new Promise((res) => setTimeout(res, 60));
      // T:11289 verbatim — see the site for why the phrase is contract.
      expect(warned).toContain("abandoned pane capture failed:");
    } finally {
      console.warn = before;
    }
  });
});

describe("captureOverview", () => {
  test("the badged encode, and the overview's own noun (T:10176)", async () => {
    const out = await captureOverview(frame, [{ x: 1, y: 2, label: "A" }], {
      strategies: { native: () => Promise.resolve(bitmap()) },
    });
    expect(out.blob).not.toBeNull();
    expect(encodes).toBeGreaterThan(0);
    encoded = null;
    const over = await captureOverview(frame, [{ x: 1, y: 2, label: "A" }], {
      strategies: { native: () => Promise.resolve(bitmap()) },
    });
    expect(over.why).toBe("no overview screenshot: it did not fit the 921600-byte budget");
  });

  test("A THROW IS A CONSOLE LINE HERE, and the wire gets the abandoned sentence", async () => {
    // `warnAs` is the overview road's and only its (T:10240-10248): the picture
    // is an aid, the message is what matters, so a strategy that throws is not
    // news for the model — it is warned under one phrase and the wire gets the
    // very sentence the TIMEOUT writes. The pane road has no `warnAs` and keeps
    // "the capture failed (…)" instead, because there the user pressed a button
    // and is standing in front of the answer (T:11291).
    const warned: unknown[][] = [];
    const before = console.warn;
    console.warn = (...args: unknown[]) => void warned.push(args);
    try {
      const out = await captureOverview(frame, [{ x: 1, y: 2, label: "A" }], {
        timeoutMs: 5,
        strategies: { native: () => Promise.reject(new Error("boom")) },
      });
      // Verbatim T:10239-10248 — the same words the timeout road produces, so
      // one refusal sentence covers both.
      expect(out.why).toBe(
        "no overview screenshot: the capture did not finish within 5ms and was abandoned",
      );
      expect(out.blob).toBeNull();
      expect(out.via).toBe("none");
      // The reason is not thrown away, it is just not the wire's business.
      expect(warned).toEqual([["overview screenshot skipped:", "boom"]]);
    } finally {
      console.warn = before;
    }
  });
});
