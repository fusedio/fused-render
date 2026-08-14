// The chord table AS A NON-MAC MACHINE SEES IT, driven from a machine that may
// well be a Mac.
//
// WHY THIS FILE EXISTS. `isMac` is decided once, at the import of lib/platform,
// from `navigator` — and platform is deliberately NOT a parameter of matchChord
// (see the note above it: re-deriving it from an argument would be a second copy
// of the app's one platform rule). The consequence is that a single test process
// can only ever exercise ONE side of every platform-split branch, and the other
// side's assertions quietly degenerate into "matchChord returned null", which is
// also what it returns for a chord it has never heard of.
//
// That is not a hypothetical. `expect(matchChord(ev("y", {ctrl: true}), listing))
// .toBe(isMac ? null : "redo")` looks like it covers Ctrl+Y on both platforms and
// covers it on NEITHER when run on a Mac: isMod requires Meta and rejects Ctrl
// there, so the event never reaches the Y branch at all and the observed null
// comes from the final fallthrough. Deleting the branch, or inverting its
// `!isMac`, left that assertion green.
//
// So the non-mac side is exercised in a CHILD PROCESS whose `navigator` reports
// Linux before lib/platform is imported. It is the only way to get a second
// value of a module-init constant without either injecting the platform (which
// the design rules out) or mutating the module registry for every other test
// file in this process. Verified to fail — not merely to pass — by breaking the
// Y branch both ways: deleted it, `Ctrl+Y is the redo chord off macOS` failed
// with "expected redo, got null"; inverted its `!isMac` to `isMac`, the same
// test failed the same way while the mac-side assertion in shortcut-chord.test.ts
// stayed green, which is exactly the blind spot this file closes.
import { expect, test } from "bun:test";
import { join } from "node:path";

// frontend/, so the child resolves this repo's tsconfig paths (@platform/…).
const FRONTEND = join(import.meta.dir, "..", "..", "..", "..");

// Ask the child for one matchChord answer. The stub goes in before the
// (therefore dynamic) import, since lib/platform reads navigator at module init.
function offMac(key: string, mods: string, ctx: string): string | null {
  const probe = `
    Object.defineProperty(globalThis, "navigator", {
      value: { platform: "Linux x86_64", userAgent: "Mozilla/5.0 (X11; Linux x86_64)" },
      configurable: true,
    });
    const { isMac } = await import("@platform/lib/platform");
    const { matchChord } = await import("@apps/explorer/listing/shortcut-chord");
    // If this ever fires, the stub stopped working and every assertion below
    // would be testing the mac table again — the exact failure mode this file
    // exists to prevent, so it is loud rather than silent.
    if (isMac) throw new Error("the navigator stub did not take: isMac is still true");
    const ev = { key: ${JSON.stringify(key)}, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, ...${mods} };
    console.log(JSON.stringify(matchChord(ev, ${ctx})));
  `;
  const out = Bun.spawnSync([process.execPath, "-e", probe], { cwd: FRONTEND });
  if (out.exitCode !== 0) throw new Error(out.stderr.toString() || "probe failed");
  return JSON.parse(out.stdout.toString().trim());
}

const LISTING = "{ inSearch: false, hasSelection: false }";
const SEARCHING = "{ inSearch: true, hasSelection: false }";
const CTRL = "{ ctrlKey: true }";

test("Ctrl+Y is the redo chord off macOS", () => {
  // The Windows/Linux redo, which the mac-side test can only ever observe as a
  // null indistinguishable from "not a chord at all".
  expect(offMac("y", CTRL, LISTING)).toBe("redo");
});

test("Ctrl+Y keeps the field's own redo while typing", () => {
  // The same guard the Mod+Z pair has, on the branch a Mac cannot reach.
  expect(offMac("y", CTRL, SEARCHING)).toBeNull();
});

test("Ctrl+Z / Ctrl+Shift+Z are undo and redo there too", () => {
  expect(offMac("z", CTRL, LISTING)).toBe("undo");
  expect(offMac("Z", "{ ctrlKey: true, shiftKey: true }", LISTING)).toBe("redo");
  expect(offMac("z", CTRL, SEARCHING)).toBeNull();
});

test("Cmd+Y is nobody's redo — a Mac keyboard on Linux does not redo", () => {
  // isMod is exclusive, so the OTHER modifier is not the primary one on either
  // platform. Asserted here as well as on the mac side because "null" means
  // something here: Ctrl+Y in the very same table DOES answer.
  expect(offMac("y", "{ metaKey: true }", LISTING)).toBeNull();
});

test("the two keys the mac table splits on answer the non-mac way", () => {
  // Bare Delete trashes and bare Backspace goes up a folder off macOS, and
  // Cmd+Backspace (the mac trash chord) is not ours there. On a Mac each of
  // these assertions is the other half of the same `isMac ? … : …` in
  // shortcut-chord.test.ts, so between the two files both halves are covered on
  // whichever machine runs them.
  expect(offMac("Delete", "{}", LISTING)).toBe("trash");
  expect(offMac("Backspace", "{}", LISTING)).toBe("parent");
  expect(offMac("Backspace", CTRL, LISTING)).toBeNull();
});
