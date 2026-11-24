// THE CANVAS PANE'S PULL OFF THE CLAUDE RAIL (CanvasWorkspace `onRailPull`),
// as arithmetic: the explorer's reopen pull (SideChrome `startSideReopenPull`)
// measured against the canvas row. Kept out of the component so the pull and
// its release can be checked without a DOM.
import { reopenWidth } from "@platform/lib/panel-drag";

/** The row as one pass of the pull sees it. */
export interface RailPullRow {
  /** The row's right edge and width (`getBoundingClientRect`). */
  right: number;
  width: number;
  /** What a standing rail takes off the row's right edge once the pane is
   *  open (rail-peek-lib `railInset`) — 0 before the open, when the rail is
   *  what stands under the pointer and `closedWidth` says so instead. */
  inset: number;
  /** What stands under the pointer while shut: the rail's 48px. */
  closedWidth: number;
  /** The pane's floor and the canvas's (CanvasWorkspace PANE_MIN_W / CANVAS_MIN_W). */
  paneMin: number;
  canvasMin: number;
}

/** The pane's width at pointer `x`, and the canvas's share of the row
 *  (`leftFrac`) that gives it — or null while the pull means nothing yet
 *  (short of OPEN_PULL while shut) or the row is too narrow for any pane.
 *  Once `opened`, the pane ends at a standing rail's edge, not the row's, and
 *  the canvas takes what the pane and the rail leave. */
export function railPullAt(x: number, opened: boolean, row: RailPullRow): { width: number; frac: number } | null {
  if (row.width <= 0) return null;
  const max = row.width - row.inset - row.canvasMin;
  if (max <= 0) return null;
  const min = Math.min(row.paneMin, max);
  const implied = row.right - x;
  const width = opened
    ? Math.min(max, Math.max(min, implied - row.inset))
    : reopenWidth(implied, row.closedWidth, min, max);
  if (width === null) return null;
  return { width, frac: 1 - (width + row.inset) / row.width };
}
