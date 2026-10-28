import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";

const { cropToPane, frameCovered, nativeCanvasShot, nativeShotAllowed, shotGateOf } =
  await import("./canvas-shot-native");

test("a screen shot is allowed only when it would show the canvas", () => {
  expect(nativeShotAllowed({ visible: true, focused: true, covered: false })).toBe(true);
  // Hidden document: the screen shows something else.
  expect(nativeShotAllowed({ visible: false, focused: true, covered: false })).toBe(false);
  // Another app in front — the agent's tool fires with no gesture behind it.
  expect(nativeShotAllowed({ visible: true, focused: false, covered: false })).toBe(false);
  // A viewer modal or a dialog over the frame.
  expect(nativeShotAllowed({ visible: true, focused: true, covered: true })).toBe(false);
});

test("covered = something else is topmost at the frame's centre, or nothing is", () => {
  const frame = { getBoundingClientRect: () => ({ left: 10, top: 20, width: 400, height: 300 }) };
  const asFrame = frame as unknown as Element;
  const docWith = (hit: Element | null) => ({
    elementFromPoint: (x: number, y: number) => {
      expect(x).toBe(210);
      expect(y).toBe(170);
      return hit;
    },
  });
  expect(frameCovered(frame, docWith(asFrame))).toBe(false);
  expect(frameCovered(frame, docWith({} as Element))).toBe(true);
  expect(frameCovered(frame, docWith(null))).toBe(true);
  // A frame with no box is not on screen.
  const flat = { getBoundingClientRect: () => ({ left: 0, top: 0, width: 0, height: 0 }) };
  expect(frameCovered(flat, docWith(flat as unknown as Element))).toBe(true);
});

test("the user's own click is proof of focus; the agent's capture asks the document", () => {
  const frame = {
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 100, height: 100 }),
  } as unknown as HTMLIFrameElement;
  const doc = (focus: boolean) =>
    ({
      visibilityState: "visible",
      hasFocus: () => focus,
      elementFromPoint: () => frame as unknown as Element,
    }) as unknown as Document;
  expect(shotGateOf(frame, "user", doc(false)).focused).toBe(true);
  expect(shotGateOf(frame, "agent", doc(false)).focused).toBe(false);
  expect(shotGateOf(frame, "agent", doc(true)).focused).toBe(true);
  expect(shotGateOf(frame, "agent", doc(true)).covered).toBe(false);
});

test("the crop is clamped to the pane; a sliver or a mostly-off-screen node is refused", () => {
  const pane = { width: 1000, height: 800 };
  // The graph element, a scrollbar's width past the frame edge: clamped.
  expect(cropToPane({ x: 0, y: 0, w: 1010, h: 800 }, pane)).toEqual({ left: 0, top: 0, width: 1000, height: 800 });
  // The graph beside the workbench's own right pane: kept as is.
  expect(cropToPane({ x: 0, y: 48, w: 700, h: 752 }, pane)).toEqual({ left: 0, top: 48, width: 700, height: 752 });
  // A node fully inside.
  expect(cropToPane({ x: 120.4, y: 200.6, w: 300, h: 200 }, pane)).toEqual({ left: 120, top: 201, width: 300, height: 200 });
  // A node mostly off the right edge: what remains is not the node.
  expect(cropToPane({ x: 900, y: 100, w: 300, h: 200 }, pane)).toBeNull();
  // A collapsed node; no box at all.
  expect(cropToPane({ x: 10, y: 10, w: 30, h: 30 }, pane)).toBeNull();
  expect(cropToPane(null, pane)).toBeNull();
  // A node drawn small at a far zoom: the embed road's full-size picture
  // beats a 90-px thumbnail, so the fast road declines it.
  expect(cropToPane({ x: 188, y: 574, w: 90, h: 60 }, pane, 320)).toBeNull();
  expect(cropToPane({ x: 100, y: 100, w: 400, h: 300 }, pane, 320)).toEqual({ left: 100, top: 100, width: 400, height: 300 });
});

test("nativeCanvasShot: no frame, no crop, a shut gate or a null still all answer null — never throw", async () => {
  expect(await nativeCanvasShot(null)).toBeNull();
  const frame = { isConnected: true } as unknown as HTMLIFrameElement;
  let captures = 0;
  const capture = async () => {
    captures += 1;
    return null;
  };
  const crop = { x: 0, y: 0, w: 500, h: 400 };
  // No crop: the whole frame is not "the canvas" — the embed road draws it.
  expect(
    await nativeCanvasShot(frame, "user", {
      gate: () => ({ visible: true, focused: true, covered: false }),
      capture,
    }),
  ).toBeNull();
  expect(captures).toBe(0);
  // Gate shut: the still is never even asked for.
  expect(
    await nativeCanvasShot(frame, "agent", {
      crop,
      gate: () => ({ visible: true, focused: false, covered: false }),
      capture,
    }),
  ).toBeNull();
  expect(captures).toBe(0);
  // Gate open, platform says no: null, and the caller falls back.
  expect(
    await nativeCanvasShot(frame, "user", {
      crop,
      gate: () => ({ visible: true, focused: true, covered: false }),
      capture,
    }),
  ).toBeNull();
  expect(captures).toBe(1);
});
