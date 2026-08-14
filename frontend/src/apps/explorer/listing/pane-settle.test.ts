import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { PANE_SETTLE_MS, settleAction } from "./pane-settle";

// WHEN a moved selection reaches the pane. The cost this exists for is stated on
// the module: every pane mount is an iframe load, and for the `claude` side that
// iframe runs `agent.py` through /api/run before it can draw anything. Holding a
// cursor key down a folder of subdirectories used to spend one of those per row.
describe("settleAction", () => {
  test("a move made from rest reaches the pane at once", () => {
    // A click, or the first press of a key: nothing is pending, so waiting would
    // be latency the user can feel for no saving at all.
    expect(settleAction(PANE_SETTLE_MS)).toBe("mount");
    expect(settleAction(PANE_SETTLE_MS + 1)).toBe("mount");
    // Never moved before — the first selection of a mount.
    expect(settleAction(Infinity)).toBe("mount");
  });

  test("a move made DURING a burst waits for the row to settle", () => {
    // The held key. Each move re-arms, so the rows passed through cost nothing
    // and only the row the user stops on is mounted.
    expect(settleAction(0)).toBe("wait");
    expect(settleAction(PANE_SETTLE_MS - 1)).toBe("wait");
  });

  test("the window is the caller's to set", () => {
    expect(settleAction(100, 300)).toBe("wait");
    expect(settleAction(100, 50)).toBe("mount");
  });

  test("the default window is a settle, not a throttle", () => {
    // Short enough to feel immediate on release, long enough to cover the ~50ms
    // repeat of a held arrow key — the interval it has to swallow.
    expect(PANE_SETTLE_MS).toBeGreaterThanOrEqual(120);
    expect(PANE_SETTLE_MS).toBeLessThanOrEqual(400);
  });
});

// The listing wires it, and the wiring is what makes the saving real: the pane's
// row, its mode key and the pill's folder/file question must all read the SETTLED
// lead. Reading the live one anywhere would put the mount back — the key alone is
// enough to remount the frame.
//
// Pinned at the source because the effect needs a DOM and a React renderer this
// test setup does not have (see selection.test.ts's header).
describe("the listing feeds the pane a settled lead", () => {
  const src = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");

  test("the pane's row comes from the settled path, not from the live lead", () => {
    expect(src).toContain("useSettledLead(");
    // The row handed to the pane, the kind question behind the pill and the mode
    // key are all derived from it.
    expect(src).toContain("const paneRow = settledLead");
    expect(src).toContain("paneRow?.isDir");
  });

  test("the pane's key is built from the settled row", () => {
    const key = src.slice(src.indexOf("key={paneKey("), src.indexOf("selCount={"));
    expect(key).toContain("paneRow");
    expect(key).not.toContain("leadRow");
  });
});
