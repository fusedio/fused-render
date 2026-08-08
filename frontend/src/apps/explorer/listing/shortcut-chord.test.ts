// The listing's chord table: which key events it claims, and — the part that
// cost a real bug — which ones it must leave alone.
//
// Dispatch is testable because the decision is pure (listing/shortcut-chord.ts);
// the hook around it needs a DOM and a React renderer, and the frontend test
// setup has neither (see the DOM-free stubs in platform/lib/*.test.ts). One
// source guard survives from when this file could only read text, for the
// reload chord — see its own test.
//
// PLATFORM: `isMac` is detected once at import (lib/platform) and is FALSE in
// this process, which reports no navigator. So these run the Windows/Linux
// table — which is where the reported bug lives (Ctrl+Shift+C is devtools'
// inspect-element there) — and the mac-only branches are asserted from the
// non-mac side: Cmd+Backspace does NOT trash here, bare Backspace DOES go up.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { matchChord } from "./shortcut-chord";

// A key event as the matcher reads it. Modifiers default to "not held", so
// every test spells out exactly the ones its chord carries — which is the
// property under test.
function ev(
  key: string,
  mods: { meta?: boolean; ctrl?: boolean; shift?: boolean; alt?: boolean } = {}
) {
  return {
    key,
    metaKey: !!mods.meta,
    ctrlKey: !!mods.ctrl,
    shiftKey: !!mods.shift,
    altKey: !!mods.alt,
  };
}
const listing = { inSearch: false };
const searching = { inSearch: true };

test("the primary-modifier clipboard chords act", () => {
  expect(matchChord(ev("c", { ctrl: true }), listing)).toBe("copy");
  expect(matchChord(ev("x", { ctrl: true }), listing)).toBe("cut");
  expect(matchChord(ev("v", { ctrl: true }), listing)).toBe("paste");
  expect(matchChord(ev("d", { ctrl: true }), listing)).toBe("duplicate");
});

test("SHIFT makes them somebody else's chord, not ours", () => {
  // The bug: `e.key` for Ctrl+Shift+C is "C", the hook lowercased it, and
  // `mod && key === "c"` matched — so the listing copied its selection AND
  // called preventDefault on the devtools inspect-element chord (and on the
  // terminal-style copy). Null means the hook never touches the event.
  expect(matchChord(ev("C", { ctrl: true, shift: true }), listing)).toBeNull();
  expect(matchChord(ev("X", { ctrl: true, shift: true }), listing)).toBeNull();
  expect(matchChord(ev("V", { ctrl: true, shift: true }), listing)).toBeNull();
  expect(matchChord(ev("D", { ctrl: true, shift: true }), listing)).toBeNull();
  // Same event with the modifier reported on the key as lowercase (layouts and
  // browsers vary): still not ours.
  expect(matchChord(ev("c", { ctrl: true, shift: true }), listing)).toBeNull();
});

test("ALT makes them somebody else's chord too", () => {
  expect(matchChord(ev("c", { ctrl: true, alt: true }), listing)).toBeNull();
  expect(matchChord(ev("v", { ctrl: true, alt: true }), listing)).toBeNull();
  expect(matchChord(ev("d", { ctrl: true, alt: true }), listing)).toBeNull();
});

test("the WRONG primary modifier is not the primary modifier", () => {
  // isMod is exclusive (lib/platform): off a Mac, Meta is not Ctrl. A Mac
  // keyboard plugged into Linux must not trash files with Cmd+Backspace.
  expect(matchChord(ev("c", { meta: true }), listing)).toBeNull();
  expect(matchChord(ev("c", { ctrl: true, meta: true }), listing)).toBeNull();
});

test("the search box keeps the native text clipboard", () => {
  expect(matchChord(ev("c", { ctrl: true }), searching)).toBeNull();
  expect(matchChord(ev("x", { ctrl: true }), searching)).toBeNull();
  expect(matchChord(ev("v", { ctrl: true }), searching)).toBeNull();
  // Duplicate is not a text chord, so it stays live while typing.
  expect(matchChord(ev("d", { ctrl: true }), searching)).toBe("duplicate");
});

test("navigation chords are bare-modifier too", () => {
  expect(matchChord(ev("ArrowDown", { ctrl: true }), listing)).toBe("open");
  expect(matchChord(ev("ArrowUp", { ctrl: true }), listing)).toBe("parent");
  // Shift+arrow with the primary modifier is a selection gesture elsewhere and
  // a text chord on macOS — never "open the lead row".
  expect(matchChord(ev("ArrowDown", { ctrl: true, shift: true }), listing)).toBeNull();
  expect(matchChord(ev("ArrowUp", { ctrl: true, alt: true }), listing)).toBeNull();
});

test("the history brackets do not steal the browser's tab chords", () => {
  expect(matchChord(ev("[", { ctrl: true }), listing)).toBe("back");
  expect(matchChord(ev("]", { ctrl: true }), listing)).toBe("forward");
  // Cmd+Shift+[ / ] switch browser tabs on macOS.
  expect(matchChord(ev("[", { ctrl: true, shift: true }), listing)).toBeNull();
  expect(matchChord(ev("]", { ctrl: true, shift: true }), listing)).toBeNull();
});

test("new folder is the one chord that WANTS Shift", () => {
  expect(matchChord(ev("N", { ctrl: true, shift: true }), listing)).toBe("new-folder");
  // …and only Shift.
  expect(matchChord(ev("N", { ctrl: true, shift: true, alt: true }), listing)).toBeNull();
  expect(matchChord(ev("n", { ctrl: true }), listing)).toBeNull();
});

test("the destructive keys are bare-key only", () => {
  expect(matchChord(ev("Delete"), listing)).toBe("trash");
  // Shift+Delete is Windows Explorer's "delete permanently" — answering it with
  // an ordinary trash would do the wrong thing under the user's own chord.
  expect(matchChord(ev("Delete", { shift: true }), listing)).toBeNull();
  expect(matchChord(ev("Delete", { alt: true }), listing)).toBeNull();
  // A held Super key reports as metaKey on Linux; isMod is false there, so a
  // `!isMod(e)` test would have let this through to a destructive branch.
  expect(matchChord(ev("Delete", { meta: true }), listing)).toBeNull();
  // Never while typing.
  expect(matchChord(ev("Delete"), searching)).toBeNull();
});

test("Backspace: up a folder here, trash only on a Mac", () => {
  expect(matchChord(ev("Backspace"), listing)).toBe("parent");
  expect(matchChord(ev("Backspace"), searching)).toBeNull();
  // The macOS trash chord, off a Mac: not ours, and not a navigation either.
  expect(matchChord(ev("Backspace", { ctrl: true }), listing)).toBeNull();
  expect(matchChord(ev("Backspace", { shift: true }), listing)).toBeNull();
});

test("rename is bare F2 — Alt+F2 is a desktop run dialog on Linux", () => {
  expect(matchChord(ev("F2"), listing)).toBe("rename");
  expect(matchChord(ev("F2", { alt: true }), listing)).toBeNull();
  expect(matchChord(ev("F2", { ctrl: true }), listing)).toBeNull();
});

// ── the reload guard ────────────────────────────────────────────────────────
// Cmd+R / Ctrl+R belongs to the browser on every platform, and `isMod` matches
// both — so a `mod && key === "r"` branch that calls preventDefault steals
// reload everywhere, in a shell whose whole UI lives in one long-running page
// where "reload" is the user's escape hatch. It used to do exactly that, to
// re-run the listing's own fetch; the folder is refreshed by the toolbar's
// Refresh control and by the watch socket, so the chord bought nothing it cost.
//
// Kept as a SOURCE guard rather than folded into the dispatch tests above:
// those prove what today's table does, while this one fails on an "r" appearing
// anywhere in the file, including in a branch nobody thought to test.
const SRC = readFileSync(join(import.meta.dir, "shortcut-chord.ts"), "utf8")
  // Comments talk ABOUT keys (this file's reasoning is quoted there), so only
  // real code counts.
  .replace(/\/\/[^\n]*/g, "")
  .replace(/\/\*[\s\S]*?\*\//g, "");

test("the listing does not hijack the browser's reload chord", () => {
  const keys = new Set<string>();
  for (const m of SRC.matchAll(/\bkey\s*===\s*"([^"]+)"/g)) keys.add(m[1]);
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
  // Nothing in the table may name it, however the branch is written.
  expect(SRC).not.toMatch(/"[rR]"/);
});
