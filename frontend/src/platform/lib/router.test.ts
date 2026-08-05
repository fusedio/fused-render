// The legacy-url rewrite runs in two places: module init (full page load) and
// navigateUrl (a stored bookmark/recents url clicked in-app, where no page
// load happens). A miss in the second path is invisible until an upgraded
// user clicks a pre-rename bookmark and lands on an unrecognized route.
import { expect, test } from "bun:test";

// router.ts reads `location` at module scope; bun has no DOM. Same shim as
// appEntry.test.ts (see the comment there).
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
  setTimeout: globalThis.setTimeout.bind(globalThis),
  clearTimeout: globalThis.clearTimeout.bind(globalThis),
};

const { rewriteLegacyUrl } = await import("./router");

test("legacy view/embed prefixes gain the /explorer namespace", () => {
  expect(rewriteLegacyUrl("/view/Users/me/data.csv")).toBe("/explorer/view/Users/me/data.csv");
  expect(rewriteLegacyUrl("/embed/Users/me/map.html")).toBe("/explorer/embed/Users/me/map.html");
  // Saved view params (the whole point of a bookmark url) ride along.
  expect(rewriteLegacyUrl("/view/Users/me/d?sort=size&_mode=grid")).toBe(
    "/explorer/view/Users/me/d?sort=size&_mode=grid",
  );
});

test("legacy settings sentinels map to their plain routes", () => {
  expect(rewriteLegacyUrl("/view/_home")).toBe("/apps");
  expect(rewriteLegacyUrl("/view/_prefs")).toBe("/preferences");
  expect(rewriteLegacyUrl("/view/_account")).toBe("/preferences?tab=account");
});

test("current urls pass through untouched", () => {
  expect(rewriteLegacyUrl("/explorer/view/Users/me/x")).toBe("/explorer/view/Users/me/x");
  expect(rewriteLegacyUrl("/apps/local/demo?_mode=claude_split")).toBe(
    "/apps/local/demo?_mode=claude_split",
  );
  // A file legitimately named view/ deeper in the path must not rewrite.
  expect(rewriteLegacyUrl("/explorer/view/w/view/f")).toBe("/explorer/view/w/view/f");
});
