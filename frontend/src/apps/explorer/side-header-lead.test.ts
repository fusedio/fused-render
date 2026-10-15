// Full screen is ONE top row: while the side pane is maximized neither header
// renders its SideCloseButton (the maximize button / Esc is the way out) and
// each renders an empty `.side-header-lead` for the chat's `.c-anntools` row to
// be portaled into. Both headers are big components with no mount precedent
// (Preview.share-row.test.ts), so this pins the branch in the source.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";

const read = (rel: string) => readFileSync(new URL(rel, import.meta.url), "utf8");

for (const [name, rel] of [
  ["PreviewSidebar", "./PreviewSidebar.tsx"],
  ["ListingPreviewPane", "./ListingPreviewPane.tsx"],
] as const) {
  test(`${name}: close button only when not maximized, lead slot only when maximized`, () => {
    const src = read(rel);
    const m = src.match(
      /\{maximized \? \(\s*(?:\/\/[^\n]*\n\s*)*<div className="side-header-lead c-tokens" \/>\s*\) : \(\s*<SideCloseButton[^>]*\/>\s*\)\}/,
    );
    expect(m).not.toBeNull();
    // No other, unconditional close button in the header.
    expect(src.match(/<SideCloseButton\b/g)).toHaveLength(1);
  });
}

test("ClaudeChat portals the .c-anntools row into the host's lead slot", () => {
  const src = read("../claude/ClaudeChat.tsx");
  expect(src).toContain('import { createPortal } from "react-dom";');
  expect(src).toContain('.querySelector(".side-header-lead")');
  expect(src).toContain("createPortal(row, leadHost)");
  expect(src).toContain('<div className="c-anntools" ref={stripRef}>');
});
