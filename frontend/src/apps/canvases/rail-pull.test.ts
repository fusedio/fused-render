// THE CANVAS PANE'S PULL OFF THE RAIL (rail-pull `railPullAt`), as CanvasWorkspace
// runs it: the opening pass measures with no inset (the rail is what stands
// under the pointer), the release re-settles against the rail as it then
// stands — so a pull-and-release with favourites on leaves the pane's left
// edge under the pointer, not the rail's 48px short of it.
import { describe, expect, test } from "bun:test";
import { OPEN_PULL } from "@platform/lib/panel-drag";
import { railPullAt, type RailPullRow } from "./rail-pull";

const RAIL = 48;
const row = (width: number, inset: number): RailPullRow => ({
  right: width,
  width,
  inset,
  closedWidth: RAIL,
  paneMin: 380,
  canvasMin: 320,
});
/** Where the pane's left edge lands (the canvas's share, from the row's left). */
const paneLeft = (frac: number, width: number) => frac * width;

describe("railPullAt", () => {
  test("nothing happens short of the pull", () => {
    expect(railPullAt(1600 - RAIL - OPEN_PULL + 1, false, row(1600, 0))).toBeNull();
  });

  test("the opening pass opens at the floor", () => {
    const at = railPullAt(1600 - RAIL - OPEN_PULL, false, row(1600, 0));
    expect(at?.width).toBe(380);
  });

  test("a release after the opening pass settles the pane under the pointer beside a standing rail", () => {
    const x = 1000;
    const open = railPullAt(x, false, row(1600, 0))!;
    // The opening pass: no inset, the canvas's share ends at the pointer…
    expect(paneLeft(open.frac, 1600)).toBeCloseTo(x, 6);
    // …but the rail stands past the pane now, and the release re-measures.
    const settled = railPullAt(x, true, row(1600, RAIL))!;
    expect(settled.width).toBeCloseTo(1600 - RAIL - x, 6);
    expect(paneLeft(settled.frac, 1600)).toBeCloseTo(x, 6);
  });

  test("on a narrow row the release keeps the canvas its floor beside the rail", () => {
    const w = 740;
    const settled = railPullAt(200, true, row(w, RAIL))!;
    expect(paneLeft(settled.frac, w)).toBeCloseTo(320, 6);
    expect(settled.width + RAIL + 320).toBeCloseTo(w, 6);
  });

  test("a row too narrow for any pane answers nothing", () => {
    expect(railPullAt(10, true, row(360, RAIL))).toBeNull();
    expect(railPullAt(10, false, row(0, 0))).toBeNull();
  });
});
