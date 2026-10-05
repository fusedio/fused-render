import { beforeEach, describe, expect, it } from "bun:test";

import { fitWhenVisible, isLaidOut } from "./terminalFit";
import { forgetTerminalSize, terminalSizeHint } from "./terminalSizeHint";

beforeEach(() => forgetTerminalSize());

function harness() {
  const term = { rows: 24, cols: 80 };
  let fits = 0;
  const fit = {
    fit() {
      fits += 1;
      term.rows = 13;
      term.cols = 81;
    },
  };
  return { term, fit, fits: () => fits };
}

describe("fitWhenVisible", () => {
  it("never fits or remembers a size for a zero-size (hidden) container", async () => {
    for (const box of [
      { clientWidth: 0, clientHeight: 0 },
      { clientWidth: 600, clientHeight: 0 },
      { clientWidth: 0, clientHeight: 200 },
    ]) {
      const h = harness();
      expect(isLaidOut(box)).toBe(false);
      expect(fitWhenVisible(box, h.fit, h.term)).toBe(false);
      expect(h.fits()).toBe(0);
      // Nothing remembered: with no drawer in the DOM the hint is null, not 80x24.
      expect(await terminalSizeHint()).toBeNull();
    }
  });

  it("fits and remembers once the container has a box (refit on becoming visible)", async () => {
    const box = { clientWidth: 0, clientHeight: 0 };
    const h = harness();
    expect(fitWhenVisible(box, h.fit, h.term)).toBe(false);
    box.clientWidth = 700;
    box.clientHeight = 200;
    expect(fitWhenVisible(box, h.fit, h.term)).toBe(true);
    expect(await terminalSizeHint()).toEqual({ rows: 13, cols: 81 });
  });
});
