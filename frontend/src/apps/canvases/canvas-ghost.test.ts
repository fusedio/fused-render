import { describe, expect, test } from "bun:test";
import {
  GHOST_ATTR,
  GHOST_FADE_MS,
  GHOST_HOLD_MS,
  GHOST_MESSAGE,
  ghostFile,
  ghostOver,
  ghostPhase,
  ghostSrc,
  ghostWord,
} from "./canvas-ghost";
import { shotGateOf } from "./canvas-shot-native";

describe("the ghost's address", () => {
  test("is the canvas.toml view on the clone's canvas.toml, in ghost mode", () => {
    expect(ghostSrc("/t/canvas/template.html", "/c/unstable/user/My canvas/")).toBe(
      "/render?path=%2Ft%2Fcanvas%2Ftemplate.html&_file=%2Fc%2Funstable%2Fuser%2FMy%20canvas%2Fcanvas.toml&ghost=1",
    );
    expect(ghostFile("C:\\canvases\\user\\demo\\")).toBe("C:\\canvases\\user\\demo/canvas.toml");
  });
  test("is nothing without the view or the clone", () => {
    expect(ghostSrc(undefined, "/c/demo")).toBeNull(); // an older server
    expect(ghostSrc("/t/canvas/template.html", null)).toBeNull();
    expect(ghostFile("")).toBeNull();
  });
});

describe("the ghost's word", () => {
  const file = "/c/demo/canvas.toml";
  test("counts for its own file only", () => {
    expect(ghostWord({ type: GHOST_MESSAGE, state: "ready", file }, file)).toBe("ready");
    expect(ghostWord({ type: GHOST_MESSAGE, state: "failed", file }, file)).toBe("failed");
    expect(ghostWord({ type: GHOST_MESSAGE, state: "ready", file: "/c/other/canvas.toml" }, file)).toBeNull();
  });
  test("is nothing for any other message", () => {
    expect(ghostWord({ type: "fused-embed-hello" }, file)).toBeNull();
    expect(ghostWord({ type: GHOST_MESSAGE, state: "drawing", file }, file)).toBeNull();
    expect(ghostWord("ready", file)).toBeNull();
    expect(ghostWord(null, file)).toBeNull();
  });
});

describe("where the ghost stands", () => {
  const base = { ready: false, failed: false, up: false, wasShown: false, sinceUpMs: 0 };
  test("framed but hidden until it is drawn, then shown", () => {
    expect(ghostPhase(base)).toBe("loading");
    expect(ghostPhase({ ...base, ready: true })).toBe("shown");
  });
  test("never shown when it failed (a first open has no canvas.toml)", () => {
    expect(ghostPhase({ ...base, failed: true })).toBe("gone");
    expect(ghostPhase({ ...base, failed: true, ready: true })).toBe("gone");
  });
  test("a workbench up before the ghost was shown gets none", () => {
    expect(ghostPhase({ ...base, up: true })).toBe("gone");
    expect(ghostPhase({ ...base, up: true, ready: true })).toBe("gone");
  });
  test("holds a moment past the hello, fades, and is gone", () => {
    const up = { ...base, ready: true, up: true, wasShown: true };
    expect(ghostPhase({ ...up, sinceUpMs: 0 })).toBe("shown");
    expect(ghostPhase({ ...up, sinceUpMs: GHOST_HOLD_MS - 1 })).toBe("shown");
    expect(ghostPhase({ ...up, sinceUpMs: GHOST_HOLD_MS })).toBe("leaving");
    expect(ghostPhase({ ...up, sinceUpMs: GHOST_HOLD_MS + GHOST_FADE_MS - 1 })).toBe("leaving");
    expect(ghostPhase({ ...up, sinceUpMs: GHOST_HOLD_MS + GHOST_FADE_MS })).toBe("gone");
  });
});

describe("a ghost over the frame", () => {
  // The ghost takes no pointer, so `elementFromPoint` sees through it: the
  // screenshot gate has to be told by name.
  const frameWith = (phase: string | null) => ({
    parentElement: {
      querySelector: (sel: string) =>
        phase && sel.includes(`[${GHOST_ATTR}="${phase}"]`) ? {} : null,
    },
  });
  test("counts while it is shown or fading, not before or after", () => {
    expect(ghostOver(frameWith("shown"))).toBe(true);
    expect(ghostOver(frameWith("leaving"))).toBe(true);
    expect(ghostOver(frameWith("loading"))).toBe(false);
    expect(ghostOver(frameWith(null))).toBe(false);
    expect(ghostOver({ parentElement: null })).toBe(false);
    expect(ghostOver({})).toBe(false);
  });
  test("is cover to the screenshot gate", () => {
    const rect = { left: 0, top: 0, width: 100, height: 100 };
    const frame = (phase: string | null) => {
      const f: Record<string, unknown> = { getBoundingClientRect: () => rect, ...frameWith(phase) };
      return f as unknown as HTMLIFrameElement;
    };
    const docFor = (f: HTMLIFrameElement) =>
      ({ visibilityState: "visible", hasFocus: () => true, elementFromPoint: () => f }) as unknown as Document;
    const clear = frame(null);
    expect(shotGateOf(clear, "user", docFor(clear)).covered).toBe(false);
    const ghosted = frame("shown");
    expect(shotGateOf(ghosted, "user", docFor(ghosted)).covered).toBe(true);
  });
});

