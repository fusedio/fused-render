// THE WHERE PILL'S WORDS (NewJobModal `WherePill`): `canvas › node`, cut out of
// the MIDDLE of each name once the two together are too long for a pill
// (Akshil, 2026-10-30: "have it as normal width and max width we do middle
// truncation"). A node is often `widget_<hash>` and a canvas a long slug; the
// start says what kind of thing it is and the end says which one, so the
// middle is what goes — the same reasoning as `path-fit.ts`, by characters
// rather than pixels, so the pill keeps a value pill's width and the row of
// four stays on one line.

import { ELLIPSIS } from "./path-fit";

/** Characters of name the pill shows, both names together (the ` › ` between
 *  them not counted) — about a value pill's width at the pill's 12px. */
export const WHERE_MAX_CHARS = 22;

/** The fewest characters a canvas name is cut to when a node is beside it, so
 *  a long node never squeezes the canvas down to a lone ellipsis. */
const CANVAS_MIN_CHARS = 8;

/** `s` in at most `max` characters, the middle replaced by one ellipsis. */
export function cutMiddle(s: string, max: number): string {
  // By code point, so an emoji is never split into a lone surrogate.
  const chars = Array.from(s);
  if (chars.length <= max) return s;
  if (max <= 1) return ELLIPSIS;
  const keep = max - 1;
  const head = Math.ceil(keep / 2);
  const tail = keep - head;
  return (
    chars.slice(0, head).join("") +
    ELLIPSIS +
    (tail > 0 ? chars.slice(chars.length - tail).join("") : "")
  );
}

/** The two names the pill draws, each middle-cut to share `max` characters:
 *  the canvas keeps what the node does not need, down to `CANVAS_MIN_CHARS`. */
export function whereParts(
  canvas: string,
  node: string | null | undefined,
  max: number = WHERE_MAX_CHARS,
): { canvas: string; node: string | null } {
  if (!node) return { canvas: cutMiddle(canvas, max), node: null };
  const canvasLen = Array.from(canvas).length;
  const nodeLen = Array.from(node).length;
  if (canvasLen + nodeLen <= max) return { canvas, node };
  // The floor never takes more than half the budget, so the node always
  // keeps some of its own.
  const canvasMin = Math.min(CANVAS_MIN_CHARS, Math.floor(max / 2));
  const canvasBudget = Math.min(canvasLen, Math.max(canvasMin, max - nodeLen));
  return {
    canvas: cutMiddle(canvas, canvasBudget),
    node: cutMiddle(node, max - canvasBudget),
  };
}
