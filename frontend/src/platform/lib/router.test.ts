// The legacy-url rewrite runs in two places: module init (full page load) and
// navigateUrl (a stored bookmark/recents url clicked in-app, where no page
// load happens). A miss in the second path is invisible until an upgraded
// user clicks a pre-rename bookmark and lands on an unrecognized route.
import { describe, expect, test } from "bun:test";

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

const { navigate, rewriteLegacyUrl } = await import("./router");

// The url navigate() pushed, with the page sitting on `search` when it ran.
// navigate reads location.search live (the framing flag is carried FORWARD, not
// re-read from a boot-time constant), so the shim's search is the whole input.
function pushedFrom(search: string, run: () => void): string {
  const loc = globalThis.location as { search: string };
  const hist = globalThis.history as {
    pushState: (state: unknown, title: string, url: string) => void;
  };
  const prevSearch = loc.search;
  const prevPush = hist.pushState;
  let pushed = "";
  loc.search = search;
  hist.pushState = (_state, _title, url) => {
    pushed = url;
  };
  try {
    run();
  } finally {
    hist.pushState = prevPush;
    loc.search = prevSearch;
  }
  return pushed;
}

// The frozen-tree framing (`snapshot=1`) names what the PAGE is — a materialised
// snapshot embedded in a view's column — not what the destination looks like, so
// it has to survive every hop the framed listing makes. It is read once at boot
// (IS_SNAPSHOT), so the live session keeps behaving after a drop; a RELOAD or a
// copied url is where a dropped flag shows up, bringing back the breadcrumb into
// the snapshot cache's internals and a preview pane inside the preview pane.
describe("navigate carries the frozen-tree framing", () => {
  test("a folder hop inside a snapshot keeps snapshot=1", () => {
    const url = pushedFrom("?snapshot=1", () => navigate("/w/snap/docs", { isDir: true }));
    expect(url).toBe("/explorer/view/w/snap/docs?snapshot=1");
  });

  test("opening a FILE inside a snapshot keeps it too", () => {
    // The framed listing opens files as well as folders, and the breadcrumb the
    // flag suppresses is the same breadcrumb on a file view.
    const url = pushedFrom("?snapshot=1", () => navigate("/w/snap/a.md", { isDir: false }));
    expect(url).toBe("/explorer/view/w/snap/a.md?snapshot=1");
  });

  test("it rides alongside the sticky pane mode and an explicit mode", () => {
    const url = pushedFrom("?snapshot=1&_panelMode=code&sort=size", () =>
      navigate("/w/snap/docs", { isDir: true, mode: "claude" }),
    );
    expect(url).toContain("snapshot=1");
    expect(url).toContain("_panelMode=code");
    expect(url).toContain("_mode=claude");
    // Ordinary view params are still dropped by a hop.
    expect(url).not.toContain("sort=size");
  });

  test("an ordinary page invents no flag", () => {
    expect(pushedFrom("?sort=size", () => navigate("/w/docs", { isDir: true }))).toBe(
      "/explorer/view/w/docs",
    );
    // Only the exact "1" is the framing flag, same as IS_SNAPSHOT reads it.
    expect(pushedFrom("?snapshot=0", () => navigate("/w/docs", { isDir: true }))).toBe(
      "/explorer/view/w/docs",
    );
  });
});

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
  expect(rewriteLegacyUrl("/apps/local/demo?_mode=claude")).toBe(
    "/apps/local/demo?_mode=claude",
  );
  // A file legitimately named view/ deeper in the path must not rewrite.
  expect(rewriteLegacyUrl("/explorer/view/w/view/f")).toBe("/explorer/view/w/view/f");
});
