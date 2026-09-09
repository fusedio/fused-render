// The field's own mode chip. No DOM in this suite (same text-parsing
// pattern as search-bar-expand.test.ts and search-clear-button.test.ts):
// read SearchField.tsx and explorer.css as text — the box's own markup, and
// both hosts (Listing.tsx over a folder, FileSearchField.tsx over a plain
// file) render this same component rather than each carrying a copy.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const CSS = readFileSync(join(import.meta.dir, "../../../styles/explorer.css"), "utf8").replace(
  /\/\*[\s\S]*?\*\//g,
  "",
);
const LISTING = readFileSync(join(import.meta.dir, "../SearchField.tsx"), "utf8");

function rulesFor(selectorExact: string): string[] {
  const out: string[] = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(CSS)) !== null) {
    if (m[1].trim() === selectorExact) out.push(m[2]);
  }
  return out;
}

// The chip's mode is driven off the same predicate introduced for "is the
// field just holding the open folder's (or file's parent's) own path" — not
// a second, parallel test for "is this a search". `searching` (non-empty
// query, useListingSearch.ts) is layered on top only because
// `queryNamesOpenFolder` itself is false for an empty query too, and an
// empty field is "Path", not "Search". `isOpenFolderQuery` itself is a prop
// here — computed once per host (Listing.tsx, FileSearchField.tsx) against
// that host's own base path, so the two never call `queryNamesOpenFolder`
// with different arguments for what should be the same answer.
test("the chip's mode is queryNamesOpenFolder layered under the existing searching gate, not a second predicate", () => {
  const at = LISTING.indexOf("const chipIsSearch =");
  expect(at).toBeGreaterThan(-1);
  const line = LISTING.slice(at, LISTING.indexOf(";", at) + 1);
  expect(line).toMatch(/searching\s*&&\s*!isOpenFolderQuery/);
  const propAt = LISTING.indexOf("isOpenFolderQuery: boolean;");
  expect(propAt).toBeGreaterThan(-1);
  expect(propAt).toBeLessThan(at);
  const LISTING_HOST = readFileSync(join(import.meta.dir, "../Listing.tsx"), "utf8");
  const hostPredicateDef = LISTING_HOST.indexOf(
    "const isOpenFolderQuery = queryNamesOpenFolder(query, fsPath, home);",
  );
  expect(hostPredicateDef).toBeGreaterThan(-1);
});

test("the chip renders both words, gated on the same variable that colors it", () => {
  const at = LISTING.indexOf('className={"listing-search-mode"');
  expect(at).toBeGreaterThan(-1);
  const block = LISTING.slice(at, LISTING.indexOf("</span>", LISTING.indexOf("listing-search-mode-label", at)));
  expect(block).toMatch(/chipIsSearch\s*\?\s*"Search"\s*:\s*"Path"/);
  expect(block).toMatch(/chipIsSearch\s*\?\s*"\s*search"\s*:\s*""/);
});

// A readout, not a control: pointer-events: none (click lands on the input
// beneath, same trick the crumbs use), and no hover or cursor treatment at
// all, unlike the star's own quiet-pill pair.
test("the chip is a readout: pointer-events none, no hover state, no quiet-pill background", () => {
  const decls = rulesFor(".listing-search-mode");
  expect(decls.length).toBe(1);
  expect(decls[0]).toMatch(/pointer-events:\s*none/);
  expect(decls[0]).not.toMatch(/background/);
  expect(decls[0]).not.toMatch(/cursor/);
  expect(rulesFor(".listing-search-mode:hover").length).toBe(0);
  expect(CSS).not.toMatch(/--ctl-quiet-bg[\s\S]{0,80}\.listing-search-mode/);
});

test("the search state alone takes the accent; the path state stays muted", () => {
  const base = rulesFor(".listing-search-mode");
  expect(base.length).toBe(1);
  expect(base[0]).toMatch(/color:\s*var\(--fg-muted\)/);
  const searchState = rulesFor(".listing-search-mode.search");
  expect(searchState.length).toBe(1);
  expect(searchState[0]).toMatch(/color:\s*var\(--accent\)/);
});

// The label carries no reserved width of its own — a fixed-width label
// would leave dead space after the shorter word ("Path") that the crumbs'
// own start position (below) does not need, since that position is already
// pinned by its own fixed offset regardless of which word the chip shows.
test("the label reserves no minimum width of its own", () => {
  const decls = rulesFor(".listing-search-mode-label");
  expect(decls.length).toBe(1);
  expect(decls[0]).not.toMatch(/min-width/);
  expect(decls[0]).not.toMatch(/width:/);
});

// The crumbs/input start is a single fixed offset sized for the chip at its
// widest ("Search"), so it never shifts when the mode word changes — one
// value, shared by the two places typed text and the crumbs it stands in
// for both begin.
test("the crumbs and the input start at the same fixed offset, sized for the chip's widest state", () => {
  const crumbs = rulesFor(".listing-search-crumbs");
  expect(crumbs.length).toBe(1);
  expect(crumbs[0]).toMatch(/left:\s*67px/);
  const input = rulesFor(".listing-search-input");
  expect(input.length).toBe(1);
  expect(input[0]).toMatch(/padding:\s*6px 10px 6px 67px/);
});

// The hint: visible only at rest. `pinnedOpen` goes true the instant the
// field takes focus, so gating on `!pinnedOpen` is what makes it vanish on
// focus, not a blur listener of its own. `!hasClear` keeps it out of the
// way of the still-present clear button in the blurred-with-a-committed-
// query ("unpin") state.
test("the hint is gone the instant the field takes focus, and while a query is still there to clear", () => {
  const at = LISTING.indexOf('className="listing-search-shortcut-hint"');
  expect(at).toBeGreaterThan(-1);
  const before = LISTING.slice(Math.max(0, at - 200), at);
  expect(before).toMatch(/!pinnedOpen\s*&&\s*!hasClear\s*&&\s*\(/);
});

// The label is platform-dependent, drawn from the app's one detection
// (`isMac`, @platform/lib/platform) rather than a fresh `navigator` check —
// the chord itself is `isMod(e) && e.key.toLowerCase() === "l"`
// (Breadcrumb.tsx).
test("the hint's label comes from the app's one platform detection, not a fresh navigator check", () => {
  const at = LISTING.indexOf('<span className="listing-search-shortcut-hint"');
  expect(at).toBeGreaterThan(-1);
  const block = LISTING.slice(at, at + 300);
  expect(block).toMatch(/isMac\s*\?\s*"⌘L"\s*:\s*"Ctrl L"/);
  const importAt = LISTING.indexOf('import { isMac } from "@platform/lib/platform";');
  expect(importAt).toBeGreaterThan(-1);
  expect(block).not.toMatch(/navigator/);
});

// The app's own key-cap vocabulary (preferences.css's `.fh-ai-hint kbd`),
// not an invented style.
test("the hint's key cap matches the app's existing kbd vocabulary", () => {
  const decls = rulesFor(".listing-search-shortcut-hint kbd");
  expect(decls.length).toBe(1);
  expect(decls[0]).toMatch(/padding:\s*0 4px/);
  expect(decls[0]).toMatch(/border:\s*1px solid var\(--border\)/);
  expect(decls[0]).toMatch(/border-radius:\s*4px/);
  expect(decls[0]).toMatch(/background:\s*var\(--bg-alt\)/);
});

// Absent, not clipped, at a narrow width: a CSS container query on the box
// itself, not a measured ref — the branch already shipped a bug of exactly
// that shape (a useLayoutEffect([]) that froze on a null ref).
test("the hint disappears at a narrow box width via a container query, not a measured ref", () => {
  const boxDecls = rulesFor(".listing-search-box");
  expect(boxDecls.length).toBe(1);
  expect(boxDecls[0]).toMatch(/container-type:\s*inline-size/);
  const containerAt = CSS.indexOf("@container (max-width: 360px)");
  expect(containerAt).toBeGreaterThan(-1);
  const block = CSS.slice(containerAt, containerAt + 200);
  expect(block).toMatch(/\.listing-search-shortcut-hint\s*\{[\s\S]*display:\s*none/);
  // The hint itself carries no width measurement of its own — no ref, no
  // ResizeObserver — anywhere near its markup.
  const hintAt = LISTING.indexOf('className="listing-search-shortcut-hint"');
  const nearby = LISTING.slice(Math.max(0, hintAt - 300), hintAt + 300);
  expect(nearby).not.toMatch(/useLayoutEffect|ResizeObserver|useWidthThresholdRef/);
});

test("the hint is a decoration: pointer-events none", () => {
  // Two exact matches: the base rule and the narrow-width override nested
  // under the container query; the base rule comes first in the file.
  const decls = rulesFor(".listing-search-shortcut-hint");
  expect(decls.length).toBe(2);
  expect(decls[0]).toMatch(/pointer-events:\s*none/);
});
