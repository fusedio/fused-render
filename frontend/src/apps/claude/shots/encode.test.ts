// The budget ladder: quality first, resolution last, and the WebP latch that
// keeps a WKWebView from paying for the same discovery twice (T:9655, 9611).
//
// The canvas is a fake — bun has no DOM, and what is under test is the DECISION
// sequence, not the browser's encoder. The fake records every `toBlob` it is
// asked for and answers from a plan.
import { beforeEach, describe, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

const {
  cropRect,
  encode,
  encodeBadged,
  fit,
  resetWebpLatchForTests,
  setCanvasFactory,
  shotExt,
  webpLatch,
} = await import("./encode");

interface Ask {
  type: string | undefined;
  quality: number | undefined;
  width: number;
  height: number;
}
type Plan = (ask: Ask) => { type: string; size: number } | null;

const asks: Ask[] = [];
let drawn = 0;
let badges = 0;

/** A canvas whose only job is to record what was asked of it. Cast because the
 *  fake answers the four members `encode` touches and nothing else. */
function installCanvas(plan: Plan): void {
  setCanvasFactory(() => {
    const canvas = {
      width: 0,
      height: 0,
      getContext: () => ({
        drawImage: () => {
          drawn++;
        },
        save: () => {},
        restore: () => {},
        beginPath: () => {},
        arc: () => {
          badges++;
        },
        fill: () => {},
        stroke: () => {},
        fillText: () => {},
        fillRect: () => {},
        fillStyle: "",
        strokeStyle: "",
        lineWidth: 0,
        font: "",
        textAlign: "",
        textBaseline: "",
      }),
      toBlob: (cb: (b: Blob | null) => void, type?: string, quality?: number) => {
        const ask: Ask = { type, quality, width: canvas.width, height: canvas.height };
        asks.push(ask);
        const out = plan(ask);
        cb(out ? ({ type: out.type, size: out.size } as Blob) : null);
      },
    };
    return canvas as unknown as HTMLCanvasElement;
  });
}

const pane = { canvas: {} as CanvasImageSource };
const rect = { left: 0, top: 0, width: 1000, height: 500 };
const limits = { maxEdge: 1000, maxBytes: 100 };

beforeEach(() => {
  asks.length = 0;
  drawn = 0;
  badges = 0;
  resetWebpLatchForTests();
});

describe("fit", () => {
  test("longest edge down to maxEdge, never up (T:9598)", () => {
    expect(fit(1000, 500, 500)).toEqual({ width: 500, height: 250, scale: 0.5 });
    expect(fit(320, 100, 640)).toEqual({ width: 320, height: 100, scale: 1 });
  });

  test("edges floor at 1px — a zero-dimension canvas throws on toBlob", () => {
    expect(fit(1000, 1, 10)).toEqual({ width: 10, height: 1, scale: 0.01 });
  });
});

describe("cropRect", () => {
  test("origin floors, far edge ceils, clamped to the bitmap (T:9578)", () => {
    expect(cropRect({ left: 10.7, top: 20.2, width: 30.1, height: 40.9 }, 500, 500)).toEqual({
      left: 10,
      top: 20,
      width: 31,
      height: 42,
    });
  });

  test("nothing to look at is null (SHOT_MIN_AREA 64)", () => {
    expect(cropRect({ left: 0, top: 0, width: 4, height: 4 }, 500, 500)).toBeNull();
    expect(cropRect({ left: 0, top: 0, width: 0, height: 0 }, 500, 500)).toBeNull();
  });
});

describe("shotExt", () => {
  test("the name follows the CONTENT, not the request (T:9616)", () => {
    expect(shotExt({ type: "image/webp" } as Blob)).toBe(".webp");
    expect(shotExt({ type: "image/png" } as Blob)).toBe(".png");
    expect(shotExt(null)).toBe(".png");
  });
});

describe("encode ladder", () => {
  test("WebP q0.8 first, and it wins when it fits", async () => {
    installCanvas(() => ({ type: "image/webp", size: 50 }));
    const out = await encode(pane, rect, limits);
    expect(out?.type).toBe("image/webp");
    expect(asks).toEqual([{ type: "image/webp", quality: 0.8, width: 1000, height: 500 }]);
    expect(webpLatch()).toBe(true);
  });

  test("q0.6 is tried before any pixel is given up", async () => {
    installCanvas((a) => ({ type: "image/webp", size: a.quality === 0.8 ? 500 : 50 }));
    const out = await encode(pane, rect, limits);
    expect(out?.size).toBe(50);
    expect(asks.map((a) => a.quality)).toEqual([0.8, 0.6]);
  });

  test("PNG is tried at each size too — flat UI genuinely beats WebP", async () => {
    installCanvas((a) => ({ type: a.type === "image/webp" ? "image/webp" : "image/png", size: a.type === "image/webp" ? 500 : 40 }));
    const out = await encode(pane, rect, limits);
    expect(out?.type).toBe("image/png");
    expect(asks.map((a) => a.type)).toEqual(["image/webp", "image/webp", "image/png"]);
  });

  test("over budget at every format halves the dims, up to 3 attempts (T:9694)", async () => {
    installCanvas(() => ({ type: "image/png", size: 999 }));
    const out = await encode(pane, rect, limits);
    expect(out).toBeNull();
    // Three sizes: 1000x500, 500x250, 250x125.
    expect(asks.filter((a) => a.type === "image/png").map((a) => a.width)).toEqual([1000, 500, 250]);
    expect(drawn).toBe(3);
  });

  test("`out` reports the size the WINNING encode used, not `fit`'s guess (T:9648)", async () => {
    installCanvas((a) => ({ type: "image/png", size: a.width > 300 ? 999 : 10 }));
    const size = { width: 0, height: 0 };
    const out = await encode(pane, rect, limits, size);
    expect(out?.size).toBe(10);
    expect(size).toEqual({ width: 250, height: 125 });
  });

  test("a null from the encoder ends the ladder (T:9689)", async () => {
    installCanvas((a) => (a.type === "image/png" ? null : { type: "image/webp", size: 999 }));
    expect(await encode(pane, rect, limits)).toBeNull();
    expect(asks.filter((a) => a.type === "image/png").length).toBe(1);
  });

  test("a PNG-typed 'webp' latches OFF, and no later shot tries webp again (T:9611)", async () => {
    installCanvas((a) => ({ type: "image/png", size: a.type === "image/webp" ? 10 : 10 }));
    const first = await encode(pane, rect, limits);
    expect(first?.type).toBe("image/png");
    expect(webpLatch()).toBe(false);
    // One webp attempt, abandoned at 0.8 — 0.6 is never asked for.
    expect(asks.map((a) => a.type)).toEqual(["image/webp", "image/png"]);
    asks.length = 0;
    await encode(pane, rect, limits);
    expect(asks.map((a) => a.type)).toEqual(["image/png"]);
  });
});

describe("encodeBadged", () => {
  test("the same ladder, with the badges drawn at every size it tries (T:8033)", async () => {
    installCanvas((a) => ({ type: "image/png", size: a.width > 300 ? 999 : 10 }));
    const out = await encodeBadged(
      { canvas: {} as CanvasImageSource, width: 1000, height: 500, blanks: [], styled: 0, incomplete: false, imagesMissing: 0 },
      [
        { x: 10, y: 20, label: "A" },
        { x: 30, y: 40, label: "B" },
      ],
      limits,
    );
    expect(out?.size).toBe(10);
    // Two badges per attempt, three attempts.
    expect(badges).toBe(6);
  });
});
