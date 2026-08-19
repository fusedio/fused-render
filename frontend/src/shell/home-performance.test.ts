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
