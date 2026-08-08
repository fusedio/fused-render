// The listing's shortcut hook must not claim the browser's RELOAD chord.
//
// Cmd+R / Ctrl+R belongs to the browser on every platform, and the hook's
// modifier test (`isMod`) matches both — so a `mod && key === "r"` branch that
// calls `preventDefault()` steals reload everywhere, in a shell whose whole UI
// lives in one long-running page where "reload" is the user's escape hatch. It
// used to do exactly that, to re-run the listing's own fetch; the folder is
// refreshed by the toolbar's Refresh control and by the watch socket, so the
// chord bought nothing it cost.
//
// This is a SOURCE guard rather than a dispatch test on purpose: the handler is
// only reachable through the `document` keydown listener the hook registers in
// an effect, and the frontend test setup has no DOM and no React renderer to
// mount a hook in (see the DOM-free stubs in platform/lib/*.test.ts). What can
// be checked without one is which keys the hook's branches claim — which is
// exactly the thing that must not silently grow an "r" back.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SRC = readFileSync(join(import.meta.dir, "useListingShortcuts.ts"), "utf8")
  // Comments talk ABOUT keys (this file's own reasoning is quoted in the
  // hook's header), so only real code counts.
  .replace(/\/\/[^\n]*/g, "")
  .replace(/\/\*[\s\S]*?\*\//g, "");

// Every key literal the handler compares against, however it spells the
// comparison (`key === "c"` on the lowercased copy, `e.key === "ArrowUp"` on
// the raw event).
function claimedKeys(): Set<string> {
  const keys = new Set<string>();
  for (const m of SRC.matchAll(/\bkey\s*===\s*"([^"]+)"/g)) keys.add(m[1]);
  return keys;
}

test("the listing does not hijack the browser's reload chord", () => {
  const keys = claimedKeys();
  // The extraction works — if a rewrite changes how comparisons are spelled,
  // these fail rather than letting the assertion below pass vacuously.
  expect(keys).toContain("c");
  expect(keys).toContain("x");
  expect(keys).toContain("v");
  expect(keys).toContain("d");
  expect(keys).toContain("F2");
  // …and reload is not among them, in either case.
  expect(keys).not.toContain("r");
  expect(keys).not.toContain("R");
  // Nothing in the hook may call preventDefault for it either, however the
  // branch is written.
  expect(SRC).not.toMatch(/"[rR]"/);
});
