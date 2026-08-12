// The listing's chord table: which key events it claims, and — the part that
// cost a real bug — which ones it must leave alone.
//
// Dispatch is testable because the decision is pure (listing/shortcut-chord.ts);
// the hook around it needs a DOM and a React renderer, and the frontend test
// setup has neither (see the DOM-free stubs in platform/lib/*.test.ts). One
// source guard survives from when this file could only read text, for the
// reload chord — see its own test.
//
// PLATFORM: `isMac` is detected once at import (lib/platform) and is NOT a
// parameter of the matcher — see the note above matchChord for why re-deriving
// it from an argument would be a second copy of the app's one platform rule. So
// these tests read the SAME detection the matcher does and drive the table it
// actually built:
//
//   • the primary modifier is spelled `mod(...)`, which is Meta on a Mac and
//     Ctrl elsewhere, and `wrongMod(...)` is the other one (never the primary,
//     on either platform — that exclusivity is a rule under test);
//   • the two platform-SPLIT branches (Delete, Backspace) assert both sides,
//     picked by the same isMac.
//
// It used to hard-code Ctrl and assert "isMac is false in this process, which
// reports no navigator". That stopped being true — bun ≥1.3 exposes a
// `navigator` with a real `platform` — and the whole file went red on macOS
// while testing nothing new. Reading the detection instead of predicting it is
// what makes it run on either platform.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { isMac } from "@platform/lib/platform";
import { matchChord } from "./shortcut-chord";

// A key event as the matcher reads it. Modifiers default to "not held", so
// every test spells out exactly the ones its chord carries — which is the
// property under test.
type Mods = { meta?: boolean; ctrl?: boolean; shift?: boolean; alt?: boolean };
function ev(key: string, mods: Mods = {}) {
  return {
    key,
    metaKey: !!mods.meta,
    ctrlKey: !!mods.ctrl,
    shiftKey: !!mods.shift,
    altKey: !!mods.alt,
  };
}

// This platform's primary modifier — ⌘ on a Mac, Ctrl everywhere else — plus
// whatever else the chord carries.
const mod = (key: string, extra: Mods = {}) =>
  ev(key, isMac ? { meta: true, ...extra } : { ctrl: true, ...extra });
// The OTHER one, which is never the primary modifier (isMod is exclusive).
const wrongMod = (key: string, extra: Mods = {}) =>
  ev(key, isMac ? { ctrl: true, ...extra } : { meta: true, ...extra });
const listing = { inSearch: false, hasSelection: false };
const searching = { inSearch: true, hasSelection: false };
// Text highlighted somewhere on the page — anywhere, since this handler is
// bound on `document`.
const selecting = { inSearch: false, hasSelection: true };

test("the primary-modifier clipboard chords act", () => {
  expect(matchChord(mod("c"), listing)).toBe("copy");
  expect(matchChord(mod("x"), listing)).toBe("cut");
  expect(matchChord(mod("v"), listing)).toBe("paste");
  expect(matchChord(mod("d"), listing)).toBe("duplicate");
});

test("SHIFT makes them somebody else's chord, not ours", () => {
  // The bug: `e.key` for Ctrl+Shift+C is "C", the hook lowercased it, and
  // `mod && key === "c"` matched — so the listing copied its selection AND
  // called preventDefault on the devtools inspect-element chord (and on the
  // terminal-style copy). Null means the hook never touches the event.
  expect(matchChord(mod("C", { shift: true }), listing)).toBeNull();
  expect(matchChord(mod("X", { shift: true }), listing)).toBeNull();
  expect(matchChord(mod("V", { shift: true }), listing)).toBeNull();
  expect(matchChord(mod("D", { shift: true }), listing)).toBeNull();
  // Same event with the modifier reported on the key as lowercase (layouts and
  // browsers vary): still not ours.
  expect(matchChord(mod("c", { shift: true }), listing)).toBeNull();
});

test("ALT makes them somebody else's chord too", () => {
  expect(matchChord(mod("c", { alt: true }), listing)).toBeNull();
  expect(matchChord(mod("v", { alt: true }), listing)).toBeNull();
  expect(matchChord(mod("d", { alt: true }), listing)).toBeNull();
});

test("the WRONG primary modifier is not the primary modifier", () => {
  // isMod is exclusive (lib/platform): off a Mac, Meta is not Ctrl, and on a Mac
  // Ctrl is not Meta. A Mac keyboard plugged into Linux must not trash files
  // with Cmd+Backspace.
  expect(matchChord(wrongMod("c"), listing)).toBeNull();
  // Both at once is neither, on either platform.
  expect(matchChord(ev("c", { ctrl: true, meta: true }), listing)).toBeNull();
});

test("the search box keeps the native text clipboard", () => {
  expect(matchChord(mod("c"), searching)).toBeNull();
  expect(matchChord(mod("x"), searching)).toBeNull();
  expect(matchChord(mod("v"), searching)).toBeNull();
  // Duplicate is not a text chord, so it stays live while typing.
  expect(matchChord(mod("d"), searching)).toBe("duplicate");
});

test("navigation chords are bare-modifier too", () => {
  expect(matchChord(mod("ArrowDown"), listing)).toBe("open");
  expect(matchChord(mod("ArrowUp"), listing)).toBe("parent");
  // Shift+arrow with the primary modifier is a selection gesture elsewhere and
  // a text chord on macOS — never "open the lead row".
  expect(matchChord(mod("ArrowDown", { shift: true }), listing)).toBeNull();
  expect(matchChord(mod("ArrowUp", { alt: true }), listing)).toBeNull();
});

test("the history brackets do not steal the browser's tab chords", () => {
  expect(matchChord(mod("["), listing)).toBe("back");
  expect(matchChord(mod("]"), listing)).toBe("forward");
  // Cmd+Shift+[ / ] switch browser tabs on macOS.
  expect(matchChord(mod("[", { shift: true }), listing)).toBeNull();
  expect(matchChord(mod("]", { shift: true }), listing)).toBeNull();
});

test("new folder is the one chord that WANTS Shift", () => {
  expect(matchChord(mod("N", { shift: true }), listing)).toBe("new-folder");
  // …and only Shift.
  expect(matchChord(mod("N", { shift: true, alt: true }), listing)).toBeNull();
  expect(matchChord(mod("n"), listing)).toBeNull();
});

test("the destructive keys are bare-key only", () => {
  // Bare Delete is the Windows/Linux trash key; on macOS the ⌦ key is not the
  // trash gesture at all (Cmd+Backspace is, below).
  expect(matchChord(ev("Delete"), listing)).toBe(isMac ? null : "trash");
  // Shift+Delete is Windows Explorer's "delete permanently" — answering it with
  // an ordinary trash would do the wrong thing under the user's own chord.
  expect(matchChord(ev("Delete", { shift: true }), listing)).toBeNull();
  expect(matchChord(ev("Delete", { alt: true }), listing)).toBeNull();
  // A held Super key reports as metaKey on Linux; isMod is false there, so a
  // `!isMod(e)` test would have let this through to a destructive branch. (On a
  // Mac this IS the primary modifier, and ⌘⌦ is nobody's trash chord either.)
  expect(matchChord(ev("Delete", { meta: true }), listing)).toBeNull();
  // Never while typing.
  expect(matchChord(ev("Delete"), searching)).toBeNull();
});

test("Backspace: trash on a Mac, up a folder everywhere else", () => {
  // The two halves of the one split, asserted from whichever side this process
  // is on — and each is the OTHER platform's foot-gun, which is why neither may
  // leak across: a bare Backspace navigating away on a Mac (where ⌘⌫ deletes),
  // or ⌘⌫ deleting on Windows.
  expect(matchChord(ev("Backspace"), listing)).toBe(isMac ? null : "parent");
  expect(matchChord(mod("Backspace"), listing)).toBe(isMac ? "trash" : null);
  // Never while typing, on either platform: ⌘⌫ is "clear to start of line".
  expect(matchChord(ev("Backspace"), searching)).toBeNull();
  expect(matchChord(mod("Backspace"), searching)).toBeNull();
  expect(matchChord(ev("Backspace", { shift: true }), listing)).toBeNull();
});

test("rename is bare F2 — Alt+F2 is a desktop run dialog on Linux", () => {
  expect(matchChord(ev("F2"), listing)).toBe("rename");
  expect(matchChord(ev("F2", { alt: true }), listing)).toBeNull();
  expect(matchChord(mod("F2"), listing)).toBeNull();
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

test("selected text keeps Cmd+C, wherever it is", () => {
  // The handler is bound on `document`, and clicking a plain <span> leaves
  // activeElement as <body> — so the listing's "am I active" guard passes while
  // the user is looking at text they highlighted somewhere else. Matching copy
  // there calls preventDefault and the browser's own copy never runs, which is
  // how an error message in the download manager could be selected and never
  // copied.
  expect(matchChord(mod("c"), selecting)).toBeNull();
  // Only copy. A non-editable selection cannot be cut, so Cmd+X still means the
  // listing's cut, and paste never had a text reading here.
  expect(matchChord(mod("x"), selecting)).toBe("cut");
  expect(matchChord(mod("v"), selecting)).toBe("paste");
  // With nothing selected it is the file-path copy it always was.
  expect(matchChord(mod("c"), listing)).toBe("copy");
});
