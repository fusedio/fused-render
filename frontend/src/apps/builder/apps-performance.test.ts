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
// The card moved to platform/ui when a third app began drawing it (#765); this
// path did not move with it, so `card()` was reading a file that is no longer
// there and the assertions below failed on a clean checkout of main.
const card = () =>
  readFileSync(join(import.meta.dir, "../../platform/ui/AppThumb.tsx"), "utf8");

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
  expect(src).toContain("const [thumbRef, nearViewport, onScreen] = useNearViewport");
  expect(src).toContain("hoverPriority || onScreen");
});

// The rank getter is a ref, not state, in the SHARED hook: every card crosses
// the real viewport edge on every scroll, and Home's bookmark cards destructure
// only `[ref, near]` — as state they would re-render for a slot they never read.
test("the on-screen rank costs no render", () => {
  const src = readFileSync(
    join(import.meta.dir, "../../platform/lib/preview-start.ts"),
    "utf8",
  );
  expect(src).toContain("const visible = useRef(false);");
  expect(src).toContain("const isVisible = useCallback(() => visible.current, []);");
  expect(src).not.toContain("setVisible");
});

// The head start is for a skeleton, not for a grid that is already drawn: on a
// thin recents store /api/apps/home runs the same workspace walk as the
// catalog, so a revisit or a `nonce` refetch would pay it twice for an answer
// the setApps guard discards.
test("the fast row is skipped once a full catalog is on screen", () => {
  const src = apps();
  expect(src).toContain("const cold = useRef(catalogCache === null);");
  expect(src).toContain("if (cold.current) {");
  expect(src).toContain("cold.current = false;");
});
