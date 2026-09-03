// Where the modified-install panel is placed (SPEC §48, D674).
//
// `panelAnchor` is pure so this needs no DOM: the component only reads the
// chip's rect and hands the result to `style`. Same split as `_shell_path` in
// selffix.py — the decision is tested where it is made, and the wiring around
// it stays thin enough to read.
import { expect, test } from "bun:test";

// VersionChip reaches `@platform/lib/router` (the panel links into the report),
// and router.ts rewrites the legacy path at MODULE INIT — so pulling anything
// out of this file touches `location` before a test runs. The house `??=` shim,
// same as router.test.ts: never an assignment over an existing global and never
// a delete afterwards, because `bun test` shares one process and a file that
// overwrites these takes them out from under every other file whose own `??=`
// already ran.
(globalThis as { location?: unknown }).location ??= {
  pathname: "/",
  search: "",
  href: "http://localhost/",
};
(globalThis as { history?: unknown }).history ??= {
  state: null,
  pushState() {},
  replaceState() {},
};
(globalThis as { window?: unknown }).window ??= {
  dispatchEvent() {},
  addEventListener() {},
  removeEventListener() {},
  setTimeout: globalThis.setTimeout.bind(globalThis),
  clearTimeout: globalThis.clearTimeout.bind(globalThis),
};

// A DYNAMIC import, and the reason is the whole point of the shim above: static
// `import` is HOISTED, so it would evaluate router.ts before a single line of
// the shim ran and the assignments would be dead code. router.test.ts does the
// same for the same reason.
const { panelAnchor } = await import("./VersionChip");

// A laptop viewport, and the chip where it actually lives: the trailing slot of
// the sidebar's LAST row, a few pixels off the bottom.
const VIEW = { innerWidth: 1440, innerHeight: 900 };
const CHIP_AT_BOTTOM = { left: 120, top: 872 };

test("the panel grows UPWARD from the chip, not down off the screen", () => {
  const at = panelAnchor(CHIP_AT_BOTTOM, VIEW);

  // Anchored by its BOTTOM edge — the property that makes it survive a chip
  // sitting at the bottom of the window.
  expect(at.bottom).toBe(VIEW.innerHeight - CHIP_AT_BOTTOM.top + 6);

  // The room it actually gets: everything above the chip. A panel is allowed
  // 70vh (styles/sidebar.css), so this has to clear that or the actions at the
  // far end are unreachable — which is exactly what a top anchor did here.
  const roomAbove = VIEW.innerHeight - at.bottom;
  expect(roomAbove).toBeGreaterThanOrEqual(0.7 * VIEW.innerHeight);
});

test("a chip near the TOP still gets a panel that fits above it", () => {
  // Not the shipping layout, but the anchor must not be silently wrong if the
  // chip ever moves — it should degrade to "less room", never to "off-screen".
  const at = panelAnchor({ left: 120, top: 40 }, VIEW);
  expect(at.bottom).toBe(VIEW.innerHeight - 40 + 6);
  expect(VIEW.innerHeight - at.bottom).toBeGreaterThanOrEqual(0);
});

test("the horizontal clamp keeps a 300px panel on screen", () => {
  // The sidebar is draggable and the window can be narrow, so the chip's own
  // left edge is not automatically somewhere the panel fits.
  const narrow = { innerWidth: 360, innerHeight: 900 };
  const at = panelAnchor({ left: 300, top: 872 }, narrow);
  expect(at.left).toBe(narrow.innerWidth - 300 - 8); // 300 = PANEL_WIDTH
  expect(at.left).toBeGreaterThanOrEqual(8);

  // ...and never off the left edge either.
  expect(panelAnchor({ left: -50, top: 872 }, VIEW).left).toBe(8);
});
