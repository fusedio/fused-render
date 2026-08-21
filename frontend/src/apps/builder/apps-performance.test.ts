// Structural guards for what the /apps hub does BEFORE it can draw anything.
// Pinned as source structure, the same posture as shell/home-performance.test.ts:
// mounting the hub would need a DOM with a real clientWidth, an
// IntersectionObserver and two endpoints, and none of that is what these
// assertions are about — they are about the page not putting a skeleton in
// front of cards it could already be showing.
import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const apps = () => readFileSync(join(import.meta.dir, "Apps.tsx"), "utf8");
const card = () => readFileSync(join(import.meta.dir, "AppPreviewCard.tsx"), "utf8");

test("the hub draws the recent row while the exhaustive catalog loads", () => {
  const src = apps();
  expect(src).toContain("getHomeApps(FAST_ROW)");
  expect(src).toContain('status: "partial"');
  // The fast row must never be awaited in front of the catalog: for a user with
  // a thin recents store the server answers it with the same workspace walk.
  expect(src).not.toMatch(/await\s+getHomeApps/);
});

test("the catalog is what the chips, the count and the empty state speak for", () => {
  const src = apps();
  expect(src).toContain('const all = apps.status === "ok" ? apps.data : [];');
  expect(src).toContain('const cards = apps.status === "loading" ? [] : apps.data;');
  // A "no apps match" verdict during the partial phase would be a claim about
  // a catalog that has not arrived.
  expect(src).toContain('apps.status === "partial" ? null : (');
});

test("a failed catalog fetch keeps the partial grid instead of replacing it", () => {
  const src = apps();
  expect(src).toContain("(e: Error) => alive && setError(e.message)");
  // The error is its own state, so the grid's phase type has no error member to
  // blank the cards with.
  expect(src).not.toContain('status: "error"');
});

test("a revisit paints from the previous catalog instead of a skeleton", () => {
  const src = apps();
  expect(src).toContain("let catalogCache: AppInfo[] | null = null;");
  expect(src).toContain('catalogCache ? { status: "ok", data: catalogCache }');
});

test("preview cards rank their queued start by being on screen", () => {
  const src = card();
  expect(src).toContain("useNearViewport<HTMLSpanElement>()");
  // Through a stable getter, never a dependency: usePreviewStart's effect
  // restarts the iframe whenever its deps change, so promoting a waiting card
  // that way would tear down a running one.
  expect(src).toContain("const backgroundRank = useCallback(() => onScreen.current, []);");
  expect(src).toContain("hoverPriority || backgroundRank");
});
