// ONE NUMBER, TWO PLACES IT HAS TO BE WRITTEN. The floor is arithmetic for the
// code that clamps a drag and a length for the stylesheets that floor a flex
// item, and neither can read the other — so it is written twice with a note at
// each end, exactly as `SIDEBAR_RAIL_WIDTH` is, and pinned here so a change to
// one that misses the other fails rather than ships.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { SIDE_PANE_MIN_VAR, SIDE_PANE_MIN_WIDTH } from "./pane-metrics";

const HERE = new URL(".", import.meta.url).pathname;
const read = (rel: string) => readFileSync(join(HERE, rel), "utf8");
const TOKENS = read("../../styles/tokens.css");
const EXPLORER = read("../../styles/explorer.css");

describe("the right-hand chat pane's floor", () => {
  it("is the composer row's width, not a round number someone liked", () => {
    // The derivation is in pane-metrics.ts; what is pinned here is the band
    // Akshil set from looking at the pane (380-420) and the fact that it is no
    // longer 220, which was the figure from before the composer row carried
    // three selects.
    expect(SIDE_PANE_MIN_WIDTH).toBeGreaterThanOrEqual(380);
    expect(SIDE_PANE_MIN_WIDTH).toBeLessThanOrEqual(420);
    expect(SIDE_PANE_MIN_WIDTH).not.toBe(220);
  });

  it("says the same thing to the stylesheets", () => {
    expect(TOKENS).toContain(`${SIDE_PANE_MIN_VAR}: ${SIDE_PANE_MIN_WIDTH}px;`);
  });

  it("is what the Explorer's Claude pane floors at", () => {
    // The pane it was borrowed FROM now borrows it back, so the two cannot
    // drift apart again.
    expect(EXPLORER).toContain(`min-width: var(${SIDE_PANE_MIN_VAR});`);
    expect(EXPLORER).not.toContain("min-width: 220px;");
  });

  it("is stated once, not per theme — a width is not a colour", () => {
    expect(TOKENS.match(new RegExp(`${SIDE_PANE_MIN_VAR}:`, "g"))?.length).toBe(1);
  });

  it("is what the listing pane's ARITHMETIC floors at, not just its CSS", () => {
    // The one that got away. `pane-math.ts` held its own `PANE_MIN_W = 220`,
    // and when the CSS floor moved every clamp in that module went on computing
    // against a width the layout would not render — the divider walked away
    // from the cursor for the whole 220-400 band, for every user, flag or no
    // flag (code review, batch 3). A literal in a second file is not a floor,
    // it is a copy of one.
    //
    // Read as SOURCE rather than imported: platform may not import an app
    // (scripts/check-boundaries.mjs), and the executable half of this pin lives
    // where it is allowed to — apps/explorer/listing/pane-math.test.ts.
    expect(read("../../apps/explorer/listing/pane-math.ts")).toContain(
      "export const PANE_MIN_W = SIDE_PANE_MIN_WIDTH;",
    );
  });
});
