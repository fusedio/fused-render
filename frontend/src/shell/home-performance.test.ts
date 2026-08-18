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
  expect(home).toContain("getHomeApps(MAX_ROW)");
  expect(home).not.toContain("getApps()");
  expect(home).not.toContain("sortApps(");
});

test("Home requests the early-stopping Claude session row", () => {
  const home = readFileSync(join(import.meta.dir, "Home.tsx"), "utf8");
  expect(home).toContain("getHomeClaudeSessionFolders(MAX_ROW)");
  expect(home).not.toContain("getClaudeSessionFolders()");
});
