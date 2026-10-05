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
  readFileSync(join(import.meta.dir, "../../platform/ui/AppPreviewCard.tsx"), "utf8");

test("the hub asks the server for one page, never the whole catalog", () => {
  const src = apps();
  expect(src).toContain("getAppsPage({ offset: 0, limit");
  expect(src).toContain("const PAGE_SIZE = 24;");
  // The order is the server's: pages are appended as they arrive, and a
  // client-side sort would interleave page 2 into page 1 under the reader.
  expect(src).not.toContain("sortApps(");
  expect(src).not.toContain("getApps()");
});

test("the chips, the count and the empty state speak for the whole catalog", () => {
  const src = apps();
  // Chip rows ride on every page from the server rather than being derived
  // from the cards the client holds, which would lose options as pages came.
  expect(src).toContain("const tags = page?.tags ?? [];");
  expect(src).toContain("orderCategories(page?.categories ?? [])");
  // The count is the FILTER's, never the page's.
  expect(src).toContain("`${page.total} of ${page.total_all} apps`");
});

test("a filter change drops the old pages during render, not after paint", () => {
  const src = apps();
  // setState-during-render: an effect would commit one frame of the old
  // list's pages under the new filter first.
  expect(src).toContain("if (loaded.key !== filterKey) {");
  expect(src).toContain("setLoaded({ key: filterKey, page: firstPages.get(filterKey) ?? null });");
});

test("a failed page fetch keeps the grid instead of replacing it", () => {
  const src = apps();
  expect(src).toContain("if (!ctl.signal.aborted) setError(e.message);");
  // The error is its own state, so the page state has no error member to
  // blank the cards with.
  expect(src).not.toContain('status: "error"');
});

test("a revisit paints from the previous first page instead of a skeleton", () => {
  const src = apps();
  expect(src).toContain("const firstPages = new Map<string, AppsPage>();");
  expect(src).toContain("page: firstPages.get(filterKey) ?? null,");
});

test("the next page is requested once and appended in server order", () => {
  const src = apps();
  expect(src).toContain("if (!page || !hasMore || loadingMore) return;");
  expect(src).toContain("apps: [...cur.apps, ...res.apps]");
  // A refetch that changed the grid under an in-flight page drops that page
  // rather than appending it at a stale offset.
  expect(src).toContain("cur.apps.length !== res.offset");
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
