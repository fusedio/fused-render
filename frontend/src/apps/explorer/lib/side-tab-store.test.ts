import { describe, expect, it } from "bun:test";

// Memory only: nothing here may touch storage, and a fresh module instance (a
// reload) starts with no remembered tab.
const touched: string[] = [];
Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  writable: true,
  value: {
    getItem: (k: string) => (touched.push("get:" + k), null),
    setItem: (k: string) => void touched.push("set:" + k),
    removeItem: (k: string) => void touched.push("rm:" + k),
  } as unknown as Storage,
});

const { getSideTab, setSideTab } = await import("./side-tab-store");

describe("side-tab-store", () => {
  it("starts with no remembered tab", () => {
    expect(getSideTab()).toBeNull();
  });

  it("remembers the last tab set", () => {
    setSideTab("git");
    expect(getSideTab()).toBe("git");
    setSideTab("claude");
    expect(getSideTab()).toBe("claude");
  });

  it("setSideTab(null) forgets the tab (what landing on Home does)", () => {
    setSideTab("git");
    setSideTab(null);
    expect(getSideTab()).toBeNull();
  });

  it("never touches storage, and a reload forgets it", async () => {
    // Only what the store's OWN calls touch counts: `touched` sees every
    // localStorage call in the process, and `bun test` runs every suite in
    // one process, so a timer left by an earlier file (a debounced write)
    // could land here and fail the test for someone else's storage use (it
    // did, once, on CI). The store's calls are synchronous, so the window
    // between the two reads admits no stray timer.
    const before = touched.length;
    setSideTab("git");
    getSideTab();
    expect(touched.length).toBe(before);
    const fresh = (await import(`./side-tab-store?reload=${1}`)) as typeof import("./side-tab-store");
    expect(fresh.getSideTab()).toBeNull();
    // ...and the module names no storage at all.
    const source = await Bun.file(new URL("./side-tab-store.ts", import.meta.url)).text();
    expect(source).not.toMatch(/localStorage|sessionStorage|indexedDB/);
  });
});
