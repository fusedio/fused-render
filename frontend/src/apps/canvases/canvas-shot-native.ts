// THE FAST ROAD TO A CANVAS PICTURE: the workbench frame's LIVE PIXELS off the
// screen, the way the chat already photographs a same-origin pane
// (`shots/native-capture`, `POST /api/capture/shot-region`).
//
// The workbench frame is cross-origin, so until now every canvas picture was
// the embed's own html-to-image over the whole flow graph — every node, every
// result iframe snapshotted, every live canvas swapped for an image, fonts
// embedded — which takes seconds on a real canvas (23 s measured on a 22-node
// map canvas) and mutates the DOM the user is looking at meanwhile. The
// native still answers in a hundred milliseconds, WebGL maps included, and
// touches nothing. The chat never tried it for the canvas only because its
// own `appFrame()` disowns a frame whose document it cannot read; the HOST
// holds the frame element, and a screen shot needs only its box.
//
// Tried FIRST, for the graph and for a node. Every "cannot" answers null;
// what the caller does then is the platform's call (canvas-shot-permission.ts:
// the embed road off a Mac, a refusal naming the fix on one):
//   - the native still is off on this platform (409, remembered), or the
//     window straddles displays / the frame is partly off screen
//     (`captureNative` itself answers null then);
//   - THE PICTURE WOULD NOT BE THE CANVAS: the document is hidden, the window
//     is not the focused one (the agent's `canvas_screenshot` fires with no
//     gesture, and the user may have another app in front — a screen shot
//     then photographs THAT), or something of ours covers the frame (a viewer
//     modal over it, a dialog). `shotGateOf` / `nativeShotAllowed` are that
//     decision, pure, so it can be tested without a screen.
//
// THE STILL IS CROPPED to what was asked for. The frame holds more than the
// graph — the workbench's own right pane (code, widget config, logs) sits
// inside the same iframe — and "the canvas" means the flow graph, "udf" one
// node's output. Which pixels those are only the workbench knows, so the
// host asks it first (`fused-embed-capture` `what: "rects"`, answered in the
// frame's CSS px, which are this page's CSS px too) and crops the still to
// the answer. No answer (an older workbench build), or a node not fully on
// screen → null, and the embed road draws it instead.
import {
  captureNative,
  encode,
  isWindowShotOff,
  shotPixels,
  SHOT_VIEW_BYTES,
  SHOT_VIEW_EDGE,
  type PaneBitmap,
} from "@apps/claude/shots";
import type { CanvasShotBy } from "@apps/claude";

/** A box in the frame's CSS px, as the workbench measures it. */
export interface ShotRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Smaller than this is not a picture worth sending (a collapsed node, a
 *  pane the workbench has hidden). */
export const SHOT_CROP_MIN = 40;
/** A NODE's output off the screen is only as big as the zoom draws it: at
 *  the 15 % zoom a whole canvas fits in, a node is a 90-px thumbnail, and the
 *  embed road — which serializes the node's result at its own full size —
 *  is the better picture by far. So a UDF crop has to be at least this wide
 *  before the fast road takes it. */
export const SHOT_UDF_CROP_MIN_WIDTH = 320;

/**
 * The crop, clamped to the pane. Null when the box is not usable: no box,
 * nothing of it inside the pane, or what remains is a sliver. A box that
 * runs PAST the pane's edge is clamped rather than refused — the graph's own
 * element is the whole frame, give or take a scrollbar — but a UDF node that
 * is mostly off screen would come back as a sliver and fails the size test.
 */
export function cropToPane(
  rect: ShotRect | null | undefined,
  pane: { width: number; height: number },
  minWidth = SHOT_CROP_MIN,
): { left: number; top: number; width: number; height: number } | null {
  if (!rect) return null;
  if (rect.w < minWidth) return null;
  const left = Math.max(0, Math.round(rect.x));
  const top = Math.max(0, Math.round(rect.y));
  const right = Math.min(pane.width, Math.round(rect.x + rect.w));
  const bottom = Math.min(pane.height, Math.round(rect.y + rect.h));
  const width = right - left;
  const height = bottom - top;
  if (width < SHOT_CROP_MIN || height < SHOT_CROP_MIN) return null;
  // A node mostly outside the frame: what is left is not the node.
  if (width < rect.w * 0.6 || height < rect.h * 0.6) return null;
  return { left, top, width, height };
}

export interface ShotGate {
  /** `document.visibilityState === "visible"`. */
  visible: boolean;
  /** `document.hasFocus()` — true when this window, or any frame in it, has
   *  focus: the host window is the one in front. A capture the USER just
   *  clicked for counts as focused whatever the document says: the click
   *  is the stronger proof, and `hasFocus()` lies in a driven browser. */
  focused: boolean;
  /** Something not ours sits over the frame (`frameCovered`). */
  covered: boolean;
}

/** The road a still takes: the REGION of the screen under the frame
 *  (`shot-region`), or the WINDOW the frame lives in (`shot-window`, D1472). */
export type ShotRoad = "region" | "window";

/** Whether a still would show the canvas. The region road needs the window
 *  in front: its pixels are whatever is on the screen there. The window road
 *  draws the window itself, wherever it is, so only the document's own state
 *  matters — not visible (a web view that is not painting has nothing fresh
 *  to draw, so the picture could be stale) or something of ours over the
 *  frame (a dialog is in the window too). */
export function nativeShotAllowed(gate: ShotGate, road: ShotRoad = "region"): boolean {
  if (!gate.visible || gate.covered) return false;
  return road === "window" || gate.focused;
}

/** Our own floating layers — the rail's card, a popover card, the toast —
 *  carry this. Over the frame they may show in the picture, but they never
 *  refuse it (D1471, F3: the owner left them in, "for time being"). */
export const SHOT_OWN_ATTR = "data-shot-own";

/** Where the frame is sampled: its centre and four points a quarter in, clear
 *  of the corners where the page's own small chips sit. */
const SAMPLES: readonly (readonly [number, number])[] = [
  [0.5, 0.5],
  [0.25, 0.25],
  [0.75, 0.25],
  [0.25, 0.75],
  [0.75, 0.75],
];

/** Is something NOT OURS over the frame? A viewer modal, a dialog or a scrim at
 *  any sample point answers true (covered); one of our own floating layers
 *  (`SHOT_OWN_ATTR`) does not. Off screen — no element at a point — is
 *  covered too, and so is a frame no point reaches at all. */
export function frameCovered(
  frame: { getBoundingClientRect(): { left: number; top: number; width: number; height: number } },
  doc: { elementFromPoint(x: number, y: number): Element | null },
): boolean {
  const r = frame.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return true;
  let seen = false;
  for (const [fx, fy] of SAMPLES) {
    const hit = doc.elementFromPoint(r.left + r.width * fx, r.top + r.height * fy);
    if (hit === (frame as unknown as Element)) seen = true;
    else if (!hit?.closest?.(`[${SHOT_OWN_ATTR}]`)) return true;
  }
  return !seen;
}

export function shotGateOf(
  frame: HTMLIFrameElement,
  by: CanvasShotBy = "user",
  doc: Document = document,
): ShotGate {
  let focused = by === "user";
  if (!focused) {
    try {
      focused = doc.hasFocus();
    } catch {
      /* not a real document */
    }
  }
  return {
    visible: doc.visibilityState === "visible",
    focused,
    covered: frameCovered(frame, doc),
  };
}

/**
 * One native still of the workbench frame, encoded under the chat's view
 * budget (`SHOT_VIEW_EDGE` / `SHOT_VIEW_BYTES`, the same limits a pane
 * screenshot gets). Null for every "cannot" — the caller falls back.
 */
export async function nativeCanvasShot(
  frame: HTMLIFrameElement | null,
  by: CanvasShotBy = "user",
  opts: {
    /** The box to keep, in the frame's CSS px (the workbench's answer).
     *  REQUIRED in effect: with no crop the still is the whole frame, side
     *  pane included, which is not "the canvas" — so a missing crop answers
     *  null and the embed road draws the right thing slowly. */
    crop?: ShotRect | null;
    /** `SHOT_UDF_CROP_MIN_WIDTH` for a node; the default for the graph. */
    minWidth?: number;
    gate?: (frame: HTMLIFrameElement, by: CanvasShotBy) => ShotGate;
    capture?: typeof captureNative;
    /** Whether the window road is shut (`isWindowShotOff`). */
    windowOff?: () => boolean;
    /** THE LAST RESORT (D1472): the window road even for a HIDDEN document
     *  (minimized, fully covered, another Space), whose still is what the
     *  window last drew and may be stale — the caller says so beside the
     *  picture. Only a dialog of ours over the frame still refuses, and the
     *  region road is never taken (its pixels would be another app's). */
    stale?: boolean;
  } = {},
): Promise<Blob | null> {
  if (!frame || !frame.isConnected) return null;
  if (!opts.crop) return null;
  const gateOf = opts.gate || shotGateOf;
  const capture = opts.capture || captureNative;
  const gate = gateOf(frame, by);
  let pane: PaneBitmap | null = null;
  // THE WINDOW ROAD FIRST (D1472): the window behind another app, on another
  // Space, across two displays — still the page, ~100 ms. Shut (a 409
  // remembered: no window still on this platform) or refused, the region
  // road below as before.
  const windowOk = opts.stale ? !gate.covered : nativeShotAllowed(gate, "window");
  if (!(opts.windowOff || isWindowShotOff)() && windowOk) {
    pane = await capture(frame, undefined, { window: true });
    // A page hidden while the still was taken may not have painted it.
    if (pane && !opts.stale && !gateOf(frame, "agent").visible) return null;
  }
  if (!pane) {
    // Gated afresh: the window road took a round trip, and focus may have
    // left in it — the region still would then be the other app's pixels.
    if (opts.stale || !nativeShotAllowed(gateOf(frame, by), "region")) return null;
    pane = await capture(frame);
    if (!pane) return null;
    // The still took a round trip; a page hidden meanwhile, a dialog opened
    // over the frame — or, for the agent's gesture-less capture, a window
    // sent behind another — makes it a picture of something else. Narrows
    // the window, does not close it.
    const after = gateOf(frame, "agent");
    if (!after.visible || after.covered || (by === "agent" && !after.focused)) return null;
  }
  const box = cropToPane(opts.crop, pane, opts.minWidth);
  if (!box) return null;
  return encode(pane, box, { maxEdge: SHOT_VIEW_EDGE, maxBytes: SHOT_VIEW_BYTES });
}

/**
 * The embed road's picture under the same view budget the native road encodes
 * to (D1471, F6): html-to-image hands back a full-size PNG, which on a big
 * canvas runs past the tool's per-image cap. A blob already inside the budget,
 * or one this engine cannot decode, is returned as it is.
 */
export async function fitShotBlob(blob: Blob): Promise<Blob> {
  if (blob.size <= SHOT_VIEW_BYTES) return blob;
  const px = await shotPixels(blob);
  if (!px) return blob;
  try {
    const fitted = await encode(
      px,
      { left: 0, top: 0, width: px.width, height: px.height },
      { maxEdge: SHOT_VIEW_EDGE, maxBytes: SHOT_VIEW_BYTES },
    );
    return fitted || blob;
  } finally {
    px.free();
  }
}
