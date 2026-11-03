// Moving between the store's pages (Discover, Installed, an app) is a path
// change inside ONE mounted MarketplacePage: the header and scroll container
// survive, and the catalog is not read again. App.tsx does not key this route
// on the nav epoch.
//
// A stubbed `globalThis.fetch`, as in Preferences.render.test.tsx.
import { afterAll, afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();
const { MarketplacePage } = await import("./MarketplacePage");
const { navigateUrl } = await import("@platform/lib/router");

// The shim's window drops listeners; the page subscribes to NAV_EVENT through
// it, so give the window a working registry for this file.
type Win = {
  addEventListener: (t: string, f: (e: Event) => void) => void;
  removeEventListener: (t: string, f: (e: Event) => void) => void;
  dispatchEvent: (e: Event) => boolean;
};
const win = (globalThis as unknown as { window: Win }).window;
const realWin = { add: win.addEventListener, remove: win.removeEventListener, dispatch: win.dispatchEvent };
const listeners = new Map<string, Set<(e: Event) => void>>();
win.addEventListener = (t, f) => {
  if (!listeners.has(t)) listeners.set(t, new Set());
  listeners.get(t)!.add(f);
};
win.removeEventListener = (t, f) => void listeners.get(t)?.delete(f);
win.dispatchEvent = (e) => {
  for (const f of [...(listeners.get(e.type) ?? [])]) f(e);
  return true;
};

afterAll(() => {
  win.addEventListener = realWin.add;
  win.removeEventListener = realWin.remove;
  win.dispatchEvent = realWin.dispatch;
});

const realFetch = globalThis.fetch;
const realLocation = globalThis.location;
const realHistory = globalThis.history;
let fetched: string[] = [];

function fakeFetch(url: unknown): Promise<Response> {
  fetched.push(String(url));
  const body = { apps: [], categories: [], generated_at: 0 };
  return Promise.resolve({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as unknown as Response);
}

function setLocation(path: string) {
  const [pathname, q] = path.split("?");
  const loc = { pathname, search: q ? "?" + q : "", href: "http://localhost" + path, origin: "http://localhost", assign() {}, reload() {} };
  globalThis.location = loc as unknown as Location;
  globalThis.history = {
    state: null,
    pushState(_s: unknown, _t: string, u: string) {
      const [p2, q2] = String(u).split("?");
      loc.pathname = p2;
      loc.search = q2 ? "?" + q2 : "";
    },
    replaceState(_s: unknown, _t: string, u: string) {
      const [p2, q2] = String(u).split("?");
      loc.pathname = p2;
      loc.search = q2 ? "?" + q2 : "";
    },
  } as unknown as History;
}

beforeEach(() => {
  fetched = [];
  globalThis.fetch = fakeFetch as unknown as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
  globalThis.location = realLocation;
  globalThis.history = realHistory;
});

async function mount(path: string) {
  setLocation(path);
  let box!: ReactTestRenderer;
  await act(async () => {
    box = create(createElement(MarketplacePage));
  });
  for (let i = 0; i < 3; i++) await act(async () => { await new Promise((r) => setTimeout(r, 5)); });
  return box;
}
const header = (box: ReactTestRenderer) => box.root.findByProps({ className: "mp-header" });
const activeTab = (box: ReactTestRenderer) =>
  box.root
    .findAll((n) => n.type === "a" && n.props["aria-current"] === "page")
    .map((n) => n.props.href)
    .join();
const catalogReads = () => fetched.filter((u) => u.includes("/api/marketplace/catalog")).length;

describe("moving between pages of the Marketplace", () => {
  test("keeps the mounted header, follows the path and does not re-read the catalog", async () => {
    const box = await mount("/marketplace");
    const before = header(box);
    expect(activeTab(box)).toBe("/marketplace");
    const reads = catalogReads();
    await act(async () => {
      navigateUrl("/marketplace/installed");
    });
    expect(header(box)).toBe(before);
    expect(activeTab(box)).toBe("/marketplace/installed");
    expect(catalogReads()).toBe(reads);
  });

  test("Discover from Discover drops the filters the URL no longer names", async () => {
    const box = await mount("/marketplace?category=dev&q=foo");
    const input = () => box.root.findAll((n) => n.type === "input" && n.props.type === "search")[0];
    expect(input().props.value).toBe("foo");
    await act(async () => {
      navigateUrl("/marketplace");
    });
    expect(input().props.value).toBe("");
  });

  test("a deep link to Installed opens Installed", async () => {
    const box = await mount("/marketplace/installed");
    expect(activeTab(box)).toBe("/marketplace/installed");
  });
});
