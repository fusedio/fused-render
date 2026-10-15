import { beforeEach, describe, expect, it } from "bun:test";

// Storage-shaped stub installed BEFORE the module loads (the seed runs at load).
const cells = new Map<string, string>();
Object.defineProperty(globalThis, "localStorage", {
  configurable: true,
  writable: true,
  value: {
    getItem: (k: string) => (cells.has(k) ? (cells.get(k) as string) : null),
    setItem: (k: string, v: string) => void cells.set(k, String(v)),
    removeItem: (k: string) => void cells.delete(k),
    clear: () => cells.clear(),
    key: (i: number) => [...cells.keys()][i] ?? null,
    get length() {
      return cells.size;
    },
  } as Storage,
});

const { SIDE_MAXIMIZED_KEY, getSideMaximized, setSideMaximized, subscribeSideMaximized } =
  await import("./side-maximized-store");

let freshSeq = 0;
async function reloadStore() {
  return (await import(
    `./side-maximized-store?seed=${++freshSeq}`
  )) as typeof import("./side-maximized-store");
}

beforeEach(() => {
  cells.clear();
  setSideMaximized(false);
  cells.clear();
});

describe("side-maximized-store", () => {
  it("defaults to false", async () => {
    expect(getSideMaximized()).toBe(false);
    expect((await reloadStore()).getSideMaximized()).toBe(false);
  });

  it("round-trips set/get", () => {
    setSideMaximized(true);
    expect(getSideMaximized()).toBe(true);
    setSideMaximized(false);
    expect(getSideMaximized()).toBe(false);
  });

  it("persists across a reload and clears the key on false", async () => {
    setSideMaximized(true);
    expect(cells.get(SIDE_MAXIMIZED_KEY)).toBe("1");
    expect((await reloadStore()).getSideMaximized()).toBe(true);
    setSideMaximized(false);
    expect(cells.has(SIDE_MAXIMIZED_KEY)).toBe(false);
  });

  it("notifies subscribers on change only, and unsubscribes", () => {
    let calls = 0;
    const off = subscribeSideMaximized(() => calls++);
    setSideMaximized(true);
    expect(calls).toBe(1);
    setSideMaximized(true);
    expect(calls).toBe(1);
    off();
    setSideMaximized(false);
    expect(calls).toBe(1);
  });

  it("survives a throwing localStorage", async () => {
    const real = (globalThis as { localStorage: Storage }).localStorage;
    const boom = () => {
      throw new Error("blocked");
    };
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      writable: true,
      value: { getItem: boom, setItem: boom, removeItem: boom } as unknown as Storage,
    });
    try {
      const s = await reloadStore();
      expect(s.getSideMaximized()).toBe(false);
      expect(() => s.setSideMaximized(true)).not.toThrow();
      expect(s.getSideMaximized()).toBe(true);
    } finally {
      Object.defineProperty(globalThis, "localStorage", {
        configurable: true,
        writable: true,
        value: real,
      });
    }
  });
});
