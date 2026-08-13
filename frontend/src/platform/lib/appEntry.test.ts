// Entry resolution for app cards. The rules that matter here are the ones no
// typecheck can check: where a click lands, and whether the browser's own
// new-tab gestures still reach the anchor. A wrong answer is either a card that
// navigates somewhere unexpected or a middle-click that does nothing.
import { expect, test } from "bun:test";

import type { AppInfo } from "./api";

// appEntry pulls `navigate`/`urlForFsPath` from router.ts, which reads
// `location` at MODULE scope (IS_EMBED) — and bun's test runtime has no DOM. A
// static import is hoisted above any shim, so `location` is stubbed first and
// the module comes in dynamically after it. The stub is on globalThis, which
// every file shares — hence `??=`, and hence a shape real enough for router to
// read rather than an empty object.
(globalThis as { location?: unknown }).location ??= {
  pathname: "/",
  search: "",
  href: "http://localhost/",
};
// `openApp` really does call navigate(), which pushes history and fires the nav
// event. Stubbed rather than avoided: the assertion below is about whether the
// shell CLAIMED the click, and swapping in a fake openApp would test the fake.
// Where navigate() then lands is router.ts's business, not this module's.
(globalThis as { history?: unknown }).history ??= {
  state: null,
  pushState() {},
  replaceState() {},
};
// The stub leaks to every other suite in the run (bun shares globals), so it
// must carry what those suites' modules read off `window` — toast.ts calls
// window.setTimeout, and a bare {dispatchEvent} broke its whole file.
(globalThis as { window?: unknown }).window ??= {
  dispatchEvent() {},
  setTimeout: globalThis.setTimeout.bind(globalThis),
  clearTimeout: globalThis.clearTimeout.bind(globalThis),
};

const { entryOf, hrefFor, isBrowserHandledClick, onAppCardClick, openTargetFor } =
  await import("./appEntry");

function app(over: Partial<AppInfo> = {}): AppInfo {
  return {
    name: "demo",
    tag: "local",
    path: "/w/local/demo",
    entry_html: "/w/local/demo/index.html",
    title: null,
    ...over,
  };
}

// A React MouseEvent, reduced to what the handler reads. `preventDefault`
// records rather than mocks — the assertion is whether the shell claimed the
// click, and that IS the preventDefault call.
function click(over: Record<string, unknown> = {}) {
  let prevented = false;
  return {
    button: 0,
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    defaultPrevented: false,
    preventDefault() {
      prevented = true;
    },
    get prevented() {
      return prevented;
    },
    ...over,
  };
}

test("entryOf prefers entry and falls back to entry_html", () => {
  expect(entryOf(app({ entry: "/w/e.png", entry_html: null }))).toBe("/w/e.png");
  // An older backend sends no `entry` at all; the page must not read undefined.
  expect(entryOf(app({ entry: undefined }))).toBe("/w/local/demo/index.html");
  expect(entryOf(app({ entry: null, entry_html: null }))).toBe(null);
});

// ------------------------------------------------------- where a click lands

test("an app with a page entry opens THAT PAGE, not its folder", () => {
  // D269, the owner's rule: a folder with a top-level html IS that page, and a
  // card opens the page. It is an ordinary explorer FILE view — no `_mode`, and
  // none of the machinery D262/D264 removed (there is no app route and no app
  // template to open it in). The isDir hint is carried and false: the card knows
  // its entry is a file, so the destination paints the file scaffold at once.
  //
  // This REVERSES the previous contract, which opened the folder as a plain
  // listing; that test is this one.
  expect(openTargetFor(app())).toEqual({
    path: "/w/local/demo/index.html",
    opts: { isDir: false },
  });
});

test("an entry that is not a page opens the file itself", () => {
  // No workspace app is shaped this way today, but `entry` exists for exactly
  // this case and the fallback below must keep meaning "nothing to open". Same
  // branch as the page above now — both are files.
  expect(openTargetFor(app({ entry: "/w/local/demo/table.csv", entry_html: null })))
    .toEqual({ path: "/w/local/demo/table.csv", opts: { isDir: false } });
});

test("an app with no entry at all opens its folder", () => {
  // The one surviving folder destination: nothing to open but the listing.
  expect(openTargetFor(app({ entry: null, entry_html: null }))).toEqual({
    path: "/w/local/demo",
    opts: { isDir: true },
  });
});

test("an older server that reports only entry_html still opens the page", () => {
  // `entry` is the newer key; entryOf falls back, and the fallback must not
  // quietly degrade a card to its folder.
  expect(openTargetFor(app({ entry: undefined }))).toEqual({
    path: "/w/local/demo/index.html",
    opts: { isDir: false },
  });
});

test("a linked app opens its page like any other app", () => {
  // A linked app's folder lives OUTSIDE the workspace, which used to mean the
  // card needed the registry-resolved /apps/linked/<name> route. An explorer
  // URL is an fs path, so the entry is addressable directly and the tag stops
  // mattering to the open path at all.
  const linked = app({ tag: "linked", name: "notes", path: "/elsewhere/notes",
    entry: "/elsewhere/notes/index.html", entry_html: "/elsewhere/notes/index.html" });
  expect(hrefFor(linked)).toBe("/explorer/view/elsewhere/notes/index.html");
});

// -------------------------------------------------------------- the new tab

test("href points at the same target a left click opens", () => {
  // The whole point of building both from openTargetFor: a new tab and an
  // in-app click cannot land in different places. Every branch is an explorer
  // URL — the entry page, the single non-page file, or the entry-less folder.
  expect(hrefFor(app())).toBe("/explorer/view/w/local/demo/index.html");
  expect(hrefFor(app({ entry: "/w/local/demo/t.csv", entry_html: null }))).toBe(
    "/explorer/view/w/local/demo/t.csv",
  );
  expect(hrefFor(app({ entry: null, entry_html: null }))).toBe("/explorer/view/w/local/demo");
  // Lockstep stated as the invariant, not just as three matching literals: a
  // future branch added to openTargetFor is covered by this line.
  for (const a of [app(), app({ entry: "/w/local/demo/t.csv", entry_html: null }),
                   app({ entry: null, entry_html: null })]) {
    expect(hrefFor(a).endsWith(encodeURI(openTargetFor(a).path))).toBe(true);
  }
});

test("href encodes a path the URL codec would otherwise break on", () => {
  // A space, a `#` and a non-ASCII name all have to survive into the href —
  // an unencoded `#` would truncate the URL at the fragment.
  expect(hrefFor(app({ path: "/w/local/my app #2", entry_html: null, entry: null }))).toBe(
    "/explorer/view/w/local/my%20app%20%232",
  );
  expect(hrefFor(app({ path: "/w/local/日本", entry_html: null, entry: null }))).toBe(
    "/explorer/view/w/local/%E6%97%A5%E6%9C%AC",
  );
  // The tag/name identity never reaches the URL any more — only the path does,
  // so an app whose tag and name are hostile is encoded by the same one codec.
  // The ENTRY is the path now (D269), and it is a hostile name of its own: the
  // codec has to survive both halves, not just the folder.
  expect(hrefFor(app({
    tag: "my tag", name: "app#2", path: "/w/my tag/app#2",
    entry: "/w/my tag/app#2/my page.html", entry_html: "/w/my tag/app#2/my page.html",
  }))).toBe("/explorer/view/w/my%20tag/app%232/my%20page.html");
});

test("the browser keeps every gesture that means 'not this tab'", () => {
  // Middle-click (and any non-primary button) plus every modifier: Cmd/Ctrl for
  // a new tab, Shift for a new window, Alt for download. Intercepting any of
  // them would make the card fight the browser.
  expect(isBrowserHandledClick(click({ button: 1 }))).toBe(true);
  expect(isBrowserHandledClick(click({ button: 2 }))).toBe(true);
  expect(isBrowserHandledClick(click({ metaKey: true }))).toBe(true);
  expect(isBrowserHandledClick(click({ ctrlKey: true }))).toBe(true);
  expect(isBrowserHandledClick(click({ shiftKey: true }))).toBe(true);
  expect(isBrowserHandledClick(click({ altKey: true }))).toBe(true);
  // A plain left click is the shell's.
  expect(isBrowserHandledClick(click())).toBe(false);
});

test("a plain left click is intercepted; a modified one is left alone", () => {
  const plain = click();
  onAppCardClick(plain, app());
  expect(plain.prevented).toBe(true); // in-app navigation, no page reload

  for (const modified of [click({ button: 1 }), click({ metaKey: true }),
                          click({ ctrlKey: true }), click({ shiftKey: true })]) {
    onAppCardClick(modified, app());
    expect(modified.prevented).toBe(false); // the href does the work
  }
});

test("a click something else already handled is not hijacked", () => {
  const handled = click({ defaultPrevented: true });
  onAppCardClick(handled, app());
  expect(handled.prevented).toBe(false);
});
