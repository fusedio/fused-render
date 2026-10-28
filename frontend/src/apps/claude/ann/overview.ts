// THE SEND-TIME OVERVIEW (T:10127-10300 `annCaptureOverview` / `annOverview` /
// `annApplyOverview`).
//
// ONE picture per message with a red letter badge burned in at each note's spot,
// so the model reconciles words with pixels through one picture instead of N.
// The letters are the `label` fields the annotations block carries
// (`protocol/wire.ts formatAnnotations`), which is what lets a comment be
// matched to the thing it points at.
//
// The capture, the encode ladder and the budget all belong to `shots/` and are
// NOT re-implemented here: this module's whole job is turning notes into badge
// POINTS and naming, in the sentence the wire prints verbatim, the ones that
// could not get a badge.
import { captureOverview, type CaptureOptions } from "../shots/capture";
import type { CaptureResult, ShotBadge } from "../shots/types";
import { badgeXY, contentBox, intrinsicOf, rectOf, pointXY, type StageBox } from "./geometry";
import {
  ANN_OFFSCREEN_DETACHED,
  ANN_OFFSCREEN_SCROLLED,
  ANN_XO_SCROLL,
  type Annotation,
} from "./types";

/** `true` = this note got a badge; a string = why it did not, verbatim. */
export type AnnMark = true | string;

export interface OverviewContext {
  /** The target's document, for resolving element anchors. Null in the hosted
   *  layout with nothing marked, and in the XO layout, where the capture already
   *  answered "nothing to read". */
  doc: Document | null;
  xo: boolean;
  /**
   * The stage box the badge bounds are checked against.
   *
   * A RECORDED DIVERGENCE from T, deliberate and narrow: T:10169 checks each
   * point against `pane.width`/`pane.height` — the rasterised bitmap, which only
   * exists AFTER the capture — because it computes the badges inside the
   * capture. `shots/capture.captureOverview` takes its badges up front, so the
   * bounds come from the framed viewport instead. They are the same box: the
   * pane bitmap IS that viewport (T:6714, and there is no devicePixelRatio
   * handling anywhere in this range), and the badges are scaled by the encoder's
   * own `fit.scale` either way (`shots/encode.encodeBadged`).
   */
  stage: StageBox | null;
  resolve(c: Annotation, doc: Document | null): Element | null;
}

/** Room kept around an element in its note's crop, and the smallest crop worth
 *  looking at: a lone icon would otherwise come back as a few dozen pixels with
 *  nothing around it to place it. A point note gets a fixed box. */
const CROP_PAD = 16;
const CROP_MIN = { width: 160, height: 120 };
const CROP_POINT = { width: 320, height: 240 };

/** A box of at least `min`, centred on `rect`'s own centre when it has to grow.
 *  Not clamped to the pane: `shots/encode.cropRect` does that against the real
 *  bitmap. */
function targetOf(
  rect: { left: number; top: number; width: number; height: number },
  min: { width: number; height: number },
): NonNullable<ShotBadge["target"]> {
  const width = Math.max(rect.width, min.width);
  const height = Math.max(rect.height, min.height);
  return {
    left: rect.left + rect.width / 2 - width / 2,
    top: rect.top + rect.height / 2 - height / 2,
    width,
    height,
  };
}

/**
 * T:10145's loop, on its own so the reasons are testable without a canvas.
 *
 * The badge for an element note is its CENTRE, not the corner its pin uses: a
 * badge is ink ON the picture and a corner one straddles two elements. `iu`/`iv`
 * still win — an image click stored the exact pixel and the badge must name that
 * pixel, not the box centre.
 */
export function badgesFor(
  pending: readonly Annotation[],
  ctx: OverviewContext,
): { badges: ShotBadge[]; marks: Record<string, AnnMark> } {
  const badges: ShotBadge[] = [];
  const marks: Record<string, AnnMark> = {};
  // The cross-origin overlay's zero-scroll stand-in, exactly as the pin painter
  // uses it: point coords are overlay-relative there and the capture comes off
  // the tab, which shares that coordinate space.
  const win = (ctx.doc && ctx.doc.defaultView) || (ctx.xo ? ANN_XO_SCROLL : null);
  for (const c of pending) {
    let pt: { x: number; y: number } | null = null;
    let target: ShotBadge["target"];
    if (c.kind === "point") {
      pt = pointXY(c, win);
      if (pt) target = targetOf({ left: pt.x, top: pt.y, width: 0, height: 0 }, CROP_POINT);
    } else {
      const el = ctx.doc ? ctx.resolve(c, ctx.doc) : null;
      if (!el) {
        marks[c.id] = ANN_OFFSCREEN_DETACHED;
        continue;
      }
      const r = rectOf(el);
      pt = badgeXY(c, contentBox(el), r, !!intrinsicOf(el));
      target = targetOf(
        {
          left: r.left - CROP_PAD,
          top: r.top - CROP_PAD,
          width: r.width + 2 * CROP_PAD,
          height: r.height + 2 * CROP_PAD,
        },
        CROP_MIN,
      );
    }
    const w = ctx.stage ? ctx.stage.clientWidth : 0;
    const h = ctx.stage ? ctx.stage.clientHeight : 0;
    if (!pt || pt.x < 0 || pt.y < 0 || pt.x > w || pt.y > h) {
      marks[c.id] = ANN_OFFSCREEN_SCROLLED;
      continue;
    }
    badges.push({ x: pt.x, y: pt.y, label: c.label || "?", ...(target ? { target } : {}) });
    marks[c.id] = true;
  }
  return { badges, marks };
}

export interface OverviewResult {
  capture: CaptureResult;
  marks: Record<string, AnnMark>;
}

/**
 * The whole send-time step: badge points, then ONE bounded capture that yields
 * the overview and each note's crop of it.
 *
 * Never throws and never fails a send (T:10202): both failure shapes collapse to
 * "no shot this turn", the annotations still go out with their anchors — which
 * are what Claude edits from anyway — and the `why` sentence rides the chip and
 * the wire's `viewNote`. A thrown or timed-out capture must never cost the user
 * the message they typed. The budget, the abandoned capture's aftercare and the
 * object-URL revoke all live in `shots/capture.race`.
 */
export async function overviewFor(
  frame: HTMLIFrameElement | null,
  pending: readonly Annotation[],
  ctx: OverviewContext,
  opts: CaptureOptions = {},
): Promise<OverviewResult | null> {
  if (!pending.length) return null;
  const { badges, marks } = badgesFor(pending, ctx);
  const capture = await captureOverview(frame, badges, { ...opts, xo: ctx.xo, rectOf });
  return { capture, marks };
}

/**
 * T:10256 `annApplyOverview` — fold a finished capture into the notes about to
 * be sent.
 *
 * STALE FIELDS ARE CLEARED FIRST: a previous send that failed left its flags
 * behind, and quietly re-sending a claim about a picture of an older screen is
 * worse than none. `shot`/`shotNote` are the per-note crops of the OLD wire
 * format — a note hydrated from a param written before the overview existed may
 * still carry them, and they go for the same staleness reason, permanently.
 *
 * Returns the updated notes rather than mutating in place, so the one write goes
 * through the store (`merge`) and the chips hear about it.
 */
export function applyOverview(
  pending: readonly Annotation[],
  marks: Record<string, AnnMark> | null | undefined,
): Annotation[] {
  return pending.map((c) => {
    const next: Annotation = { ...c };
    delete next.offscreen;
    delete next.shot;
    delete next.shotNote;
    const m = marks ? marks[c.id] : undefined;
    if (typeof m === "string") next.offscreen = m;
    return next;
  });
}
