// Structural boot-path guards. Importing main.tsx would mount React and fetch;
// these assertions instead pin which document owns the global stores.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

test("Home relies on the one top-level recents hydration", () => {
  const home = readFileSync(join(import.meta.dir, "Home.tsx"), "utf8");
  const main = readFileSync(join(import.meta.dir, "../main.tsx"), "utf8");
  expect(home).not.toContain("hydrateRecents");
  expect(main.match(/hydrateRecents\(\)/g)?.length).toBe(1);
});

test("embed boot exits before global bookmark and recents hydration", () => {
  const main = readFileSync(join(import.meta.dir, "../main.tsx"), "utf8");
  const guard = main.indexOf("if (IS_EMBED) return;");
  expect(guard).toBeGreaterThan(-1);
  expect(guard).toBeLessThan(main.indexOf("hydrateBookmarks()"));
  expect(guard).toBeLessThan(main.indexOf("hydrateRecents()"));
});

test("preview thumbnails skip OS clipboard sync without disabling interactive embeds", () => {
  const app = readFileSync(join(import.meta.dir, "App.tsx"), "utf8");
  expect(app.match(/if \(!IS_PREVIEW\) void reconcileOsClipboard\(\);/g)?.length).toBe(2);
  expect(app).not.toContain("if (!IS_EMBED) void reconcileOsClipboard()");
});

test("shared explorer previews enter the scheduler only near the viewport", () => {
  const cards = readFileSync(
    join(import.meta.dir, "../apps/explorer/BookmarkCards.tsx"),
    "utf8",
  );
  expect(cards).toContain("useNearViewport<HTMLSpanElement>()");
  expect(cards).toContain("usePreviewStart(nearViewport)");
});

test("Home requests the recent-first app row instead of the exhaustive catalog", () => {
  const home = readFileSync(join(import.meta.dir, "Home.tsx"), "utf8");
  expect(home).toContain("getHomeApps(Math.min(limit, MAX_ROW))");
  expect(home).not.toContain("getApps()");
  expect(home).not.toContain("sortApps(");
});

test("Home requests the early-stopping Claude session row", () => {
  const home = readFileSync(join(import.meta.dir, "Home.tsx"), "utf8");
  expect(home).toContain("getHomeClaudeSessionFolders(Math.min(limit, MAX_ROW))");
  expect(home).not.toContain("getClaudeSessionFolders()");
});

// The row asks for the cards it can draw, because the server's fast path is
// gated on the recents FILLING the request: /api/apps/home only skips its
// exhaustive workspace walk when it has `limit` recents, so a fixed request for
// MAX_ROW=12 walked the whole workspace on every Home visit for anyone with
// fewer than twelve opened apps. Pinned as source structure rather than
// behaviour because mounting Home would need a DOM with a real clientWidth.
test("Home sizes both row requests by the measured card count, never a constant", () => {
  const home = readFileSync(join(import.meta.dir, "Home.tsx"), "utf8");
  // Unmeasured means UNKNOWN, not a guess: a fetch before the wrapper has a
  // width would ask for cards the row cannot show.
  expect(home).toContain("count: null");
  expect(home.match(/if \(limit === null\) return;/g)?.length).toBe(2);
  expect(home.match(/\}, \[limit\]\);/g)?.length).toBe(2);
  // Grow-only, so narrowing the window refetches nothing.
  expect(home).toContain("limit: Math.max(prev.limit ?? 0, fits)");
  expect(home).not.toContain("useState(3)");
});

// The two async strips used to hold a single ~18px "Loading…" line while
// their fetch was in flight, then jump to a row of ~268px cards — two
// sections each pushing the whole page down once for the fetch and once more
// if it resolved empty. A skeleton row of the same card shape holds that
// height from first paint instead.
test("Home's async sections render a skeleton row, not a bare loading line", () => {
  const home = readFileSync(join(import.meta.dir, "Home.tsx"), "utf8");
  expect(home).not.toContain("Loading apps…");
  expect(home).not.toContain("Looking for sessions…");
  expect(home.match(/apps === null \? \(\s*<SkeletonRow/)).not.toBeNull();
  expect(home.match(/sessions === null \? \(\s*<SkeletonRow/)).not.toBeNull();
});

// Pixel-identical to the row it is replaced by: a skeleton sized by a
// constant (or by `limit`, the PEAK count) would draw a different number of
// cards than the row that lands once the fetch resolves, which is exactly the
// shift this feature exists to remove.
test("the skeleton row draws exactly as many cards as the real row", () => {
  const home = readFileSync(join(import.meta.dir, "Home.tsx"), "utf8");
  expect(home.match(/<SkeletonRow count={shown}/g)?.length).toBe(2);
});

// `started` only means the scheduler admitted the navigation — a raw iframe
// mid-boot still paints its own blank/white frame before that. Gating the
// fade on a separate `loaded` (set from onLoad) is what keeps the crossfade
// from handing the shimmer off to a half-booted page.
test("LivePreview crossfades on load rather than painting a booting frame", () => {
  const cards = readFileSync(
    join(import.meta.dir, "../apps/explorer/BookmarkCards.tsx"),
    "utf8",
  );
  expect(cards).toContain("opacity: loaded ? 1 : 0");
});

// The 800px margin was set for a grid with roughly one row on screen; Home
// stacks four rows in one scroller, so it made nearly every card "near" on
// load and queued a whole embed-shell document for rows the reader had not
// scrolled to. 300px is still roughly a row of lookahead.
test("the near-viewport lookahead is the tighter one", () => {
  const previewStart = readFileSync(
    join(import.meta.dir, "../platform/lib/preview-start.ts"),
    "utf8",
  );
  expect(previewStart).toContain("300px 0px");
  expect(previewStart).not.toContain('"800px 0px"');
});
