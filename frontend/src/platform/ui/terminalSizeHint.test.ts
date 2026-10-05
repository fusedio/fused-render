// The pure parts of the new-pty size hint (terminalSizeHint.ts): validation of
// a remembered size, the grid estimate, and "remembered beats estimated".
import { beforeEach, describe, expect, it } from "bun:test";

import { estimateGrid, forgetTerminalSize, rememberTerminalSize, terminalSizeHint } from "./terminalSizeHint";

beforeEach(() => forgetTerminalSize());

describe("estimateGrid", () => {
  it("subtracts padding, scrollbar and tab strip before dividing by the cell", () => {
    // (1000 - 16 - 14) / 7 = 138.57 -> 138 cols; (400 - 16 - 34) / 15 = 23.3 -> 23 rows
    expect(estimateGrid({ width: 1000, height: 400 }, { width: 7, height: 15 })).toEqual({ rows: 23, cols: 138 });
  });

  it("is null for a hidden drawer or an unmeasurable cell", () => {
    expect(estimateGrid({ width: 0, height: 0 }, { width: 7, height: 15 })).toBeNull();
    expect(estimateGrid({ width: 1000, height: 400 }, { width: 0, height: 15 })).toBeNull();
    expect(estimateGrid({ width: 1000, height: 400 }, { width: NaN, height: 15 })).toBeNull();
  });
});

describe("terminalSizeHint", () => {
  it("returns the last fitted size", async () => {
    rememberTerminalSize(30, 120);
    expect(await terminalSizeHint()).toEqual({ rows: 30, cols: 120 });
    rememberTerminalSize(31, 121);
    expect(await terminalSizeHint()).toEqual({ rows: 31, cols: 121 });
  });

  it("ignores an invalid fitted size", async () => {
    rememberTerminalSize(30, 120);
    rememberTerminalSize(0, 80);
    rememberTerminalSize(24, 1.5);
    rememberTerminalSize(24, 70000);
    expect(await terminalSizeHint()).toEqual({ rows: 30, cols: 120 });
  });

  it("is null with nothing remembered and no drawer in the DOM", async () => {
    expect(await terminalSizeHint()).toBeNull();
  });
});
