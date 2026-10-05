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
    setSideTab("git");
    const fresh = (await import(`./side-tab-store?reload=${1}`)) as typeof import("./side-tab-store");
    expect(fresh.getSideTab()).toBeNull();
    expect(touched).toEqual([]);
  });
});
