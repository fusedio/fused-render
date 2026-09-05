// WHERE the version chip sits in the Settings row, and why that is a structural
// claim rather than a cosmetic one (SPEC §48).
//
// Read out of the source, the same way sidebar-tasks.test.ts reads the claims a
// DOM-less test cannot otherwise hold. What is being pinned is a relationship
// between two files — the row's markup in the shell and the element the chip
// becomes once an install is modified — which no unit of either half can see on
// its own, and which broke silently when #737 moved the version into this row.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const HERE = new URL(".", import.meta.url).pathname;
const SIDEBAR = readFileSync(join(HERE, "GlobalSidebar.tsx"), "utf8");
const CHIP = readFileSync(
  join(HERE, "..", "platform", "ui", "VersionChip.tsx"), "utf8");
const CSS = readFileSync(join(HERE, "..", "styles", "sidebar.css"), "utf8");

/** `PreferencesTrigger`'s returned markup. */
function triggerMarkup(): string {
  const at = SIDEBAR.indexOf("function PreferencesTrigger(");
  expect(at).toBeGreaterThan(-1);
  const open = SIDEBAR.indexOf("return (", at);
  const close = SIDEBAR.indexOf("\n}", open);
  return SIDEBAR.slice(open, close);
}

describe("the version chip's place in the Settings row", () => {
  it("is a SIBLING of the trigger button, never inside it", () => {
    // A button inside a button is invalid, and here it is worse than invalid:
    // the click meant for the report bubbles to the trigger and opens the
    // Settings menu instead, so the badge cannot do the one thing it exists
    // for. Tasks can put its count inside the button because a count is text;
    // this slot holds a control.
    const markup = triggerMarkup();
    const trail = markup.indexOf("sidebar-item-trail");
    const closesButton = markup.indexOf("</button>");
    expect(trail).toBeGreaterThan(-1);
    expect(closesButton).toBeGreaterThan(-1);
    expect(trail).toBeGreaterThan(closesButton);
  });

  it("is worth pinning because the modified chip really is a button", () => {
    // If the chip were only ever text the nesting above would not matter, so
    // this is the premise the first assertion rests on rather than a separate
    // claim about VersionChip.
    expect(CHIP).toContain("<button");
    expect(CHIP).toContain('className="version-chip is-modified"');
  });

  it("wears the pill this slot already had, not the brand row's dead class", () => {
    // `.version-chip` is the Settings row's own chip (#737). The chip used to
    // live in the brand row and carried `.brand-version`, whose rules were all
    // scoped to `.sidebar-brand` — left behind, they matched nothing and the
    // chip painted as plain body text in both states.
    expect(CHIP).not.toContain("brand-version");
    expect(CSS).not.toContain("brand-version");
    expect(CSS).toContain("button.version-chip.is-modified");
  });

  it("keeps its modified rules OUT of a `.sidebar-brand` scope", () => {
    // The specific way the last one regressed: a selector that still named the
    // row the chip had left. Anchoring on `.sidebar-brand` here is the mistake,
    // whatever the class after it is called.
    for (const line of CSS.split("\n")) {
      if (line.includes("version-chip") && line.includes("{")) {
        expect(line).not.toContain(".sidebar-brand");
      }
    }
  });
});
