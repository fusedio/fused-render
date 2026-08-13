// The right-click menu on an app card. What matters here is what no typecheck
// can check: which entries the menu offers, in which order, and — for the one
// the menu exists for — that "Open in Explorer" really lands on the app FOLDER
// in the internal explorer rather than opening the app or shelling out to the
// OS file manager.
import { expect, test } from "bun:test";

import type { AppInfo } from "./api";

// Same shim dance as appEntry.test.ts: router.ts reads `location`/`history` at
// MODULE scope, and bun's test runtime has no DOM. `??=` because the stubs are
// shared with every other suite in the run.
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

const { appCardMenu } = await import("./appCardMenu");
const { hrefFor } = await import("./appEntry");
const { MenuIcons } = await import("@platform/ui/MenuIcons");
import type { MenuEntry, MenuItem } from "@platform/ui/ContextMenu";

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

function labels(items: MenuEntry[]): string[] {
  return items.map((i) => (i === "separator" ? "separator" : i.label));
}

function itemNamed(items: MenuEntry[], label: string): MenuItem {
  const hit = items.find((i) => i !== "separator" && i.label === label);
  if (!hit || hit === "separator") throw new Error("no such item: " + label);
  return hit;
}

// Records what navigate()/openApp() push, so the entries can be activated for
// real instead of against a fake navigate. Restored after each use: the stub
// object is shared with the rest of the run.
function capturePushes<T>(body: () => T): { url: string; state: unknown }[] {
  const h = globalThis.history as unknown as {
    pushState: (s: unknown, t: string, u: string) => void;
  };
  const original = h.pushState;
  const pushes: { url: string; state: unknown }[] = [];
  h.pushState = (state, _title, url) => pushes.push({ url, state });
  try {
    body();
  } finally {
    h.pushState = original;
  }
  return pushes;
}

test("the card menu offers Finder-order entries, Open in Explorer second", () => {
  expect(labels(appCardMenu(app()))).toEqual([
    "Open",
    "Open in Explorer",
    "separator",
    "Reveal in Finder",
    "Copy Path",
  ]);
});

test("every entry carries an icon", () => {
  // The icon column is reserved for the whole group, so a single iconless row
  // reads as a hole in the menu.
  for (const item of appCardMenu(app())) {
    if (item !== "separator") expect(item.icon).toBeDefined();
  }
  expect(itemNamed(appCardMenu(app()), "Copy Path").icon).toBe(MenuIcons.copyPath);
});

test("Open in Explorer opens the app FOLDER in the internal explorer", () => {
  // Not the app (that is what "Open" does) and not the OS file manager (that is
  // "Reveal in Finder"): the explorer listing of the app's own directory, with
  // the isDir nav hint so the destination paints the listing scaffold at once.
  const pushes = capturePushes(() => itemNamed(appCardMenu(app()), "Open in Explorer").onClick!());
  expect(pushes).toEqual([{ url: "/explorer/view/w/local/demo", state: { fsDir: true } }]);
});

test("Open goes where a left click on the card goes", () => {
  // Same target as hrefFor — the app's ENTRY PAGE (D269), which is a file, so
  // the nav carries no isDir scaffold hint. "Open in Explorer" above is the
  // entry that is always the folder, and this asserts they now differ.
  const pushes = capturePushes(() => itemNamed(appCardMenu(app()), "Open").onClick!());
  expect(pushes).toEqual([{ url: hrefFor(app()), state: { fsDir: false } }]);
  expect(hrefFor(app())).toBe("/explorer/view/w/local/demo/index.html");
});
