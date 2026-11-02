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
import { captureNative, encode, SHOT_VIEW_BYTES, SHOT_VIEW_EDGE } from "@apps/claude/shots";
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
  /** Something other than the frame sits over the frame's centre. */
  covered: boolean;
}

/** Whether a screen shot of the frame's box would show the canvas. */
export function nativeShotAllowed(gate: ShotGate): boolean {
  return gate.visible && gate.focused && !gate.covered;
}

/** Is the frame the topmost thing at its own centre? A viewer modal, a dialog
 *  or a scrim over it answers true (covered). Off screen — no element at that
 *  point at all — is covered too: the still would not show the frame. */
export function frameCovered(
  frame: { getBoundingClientRect(): { left: number; top: number; width: number; height: number } },
  doc: { elementFromPoint(x: number, y: number): Element | null },
): boolean {
  const r = frame.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return true;
  const hit = doc.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return hit !== (frame as unknown as Element);
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
  } = {},
): Promise<Blob | null> {
  if (!frame || !frame.isConnected) return null;
  if (!opts.crop) return null;
  const gate = (opts.gate || shotGateOf)(frame, by);
  if (!nativeShotAllowed(gate)) return null;
  const pane = await (opts.capture || captureNative)(frame);
  if (!pane) return null;
  // The still took a round trip; a page hidden meanwhile — or, for the
  // agent's gesture-less capture, a window sent behind another — makes it a
  // picture of something else. Narrows the window, does not close it.
  const after = (opts.gate || shotGateOf)(frame, "agent");
  if (!after.visible || (by === "agent" && !after.focused)) return null;
  const box = cropToPane(opts.crop, pane, opts.minWidth);
  if (!box) return null;
  return encode(pane, box, { maxEdge: SHOT_VIEW_EDGE, maxBytes: SHOT_VIEW_BYTES });
}
