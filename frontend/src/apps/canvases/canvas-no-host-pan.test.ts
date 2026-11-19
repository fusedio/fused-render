// THE HOST NEVER PANS THE WORKBENCH. The Claude pane shares the row with the
// frame (#1875), so every change of its width resizes the frame, and the
// workbench's own ResizeObserver (studio-canvas) shifts the canvas by half the
// change to keep its visible centre put — as it does for a window resize or
// its code pane. The seam drag and the rail pull also posted
// `fused-embed-pan` with that same half, so the canvas moved twice: a 400px
// drag put the centre 200px off.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const WORKSPACE = readFileSync(join(import.meta.dir, "CanvasWorkspace.tsx"), "utf8");

describe("CanvasWorkspace pane resize", () => {
  it("posts no fused-embed-pan to the frame", () => {
    expect(WORKSPACE).not.toMatch(/type:\s*"fused-embed-pan"/);
  });
});
