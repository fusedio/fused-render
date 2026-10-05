import { beforeEach, describe, expect, it } from "bun:test";

// bun's test runner has no DOM, so the store's one browser dependency is
// supplied here — a Storage-shaped object installed BEFORE the module is
// imported, because the seed runs at load.
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

const { SIDE_HIDDEN_KEY, getSideHidden, setSideHidden } = await import("./side-hidden-store");

let freshSeq = 0;
async function reloadStore() {
  return (await import(
    `./side-hidden-store?seed=${++freshSeq}`
  )) as typeof import("./side-hidden-store");
}

beforeEach(() => {
  cells.clear();
  setSideHidden(false);
  cells.clear();
});

describe("side-hidden-store", () => {
  it("starts clean", () => {
    expect(getSideHidden()).toBe(false);
  });

  it("remembers a close", () => {
    setSideHidden(true);
    expect(getSideHidden()).toBe(true);
  });

  it("clears on a reopen", () => {
    setSideHidden(true);
    setSideHidden(false);
    expect(getSideHidden()).toBe(false);
  });
});

describe("side-hidden-store persistence", () => {
  it("a close is written to storage", () => {
    setSideHidden(true);
    expect(localStorage.getItem(SIDE_HIDDEN_KEY)).toBe("1");
  });

  it("a reopen removes the key", () => {
    setSideHidden(true);
    setSideHidden(false);
    expect(localStorage.getItem(SIDE_HIDDEN_KEY)).toBeNull();
  });

  it("a stored close is the answer at module load (survives a reload)", async () => {
    localStorage.setItem(SIDE_HIDDEN_KEY, "1");
    const fresh = await reloadStore();
    expect(fresh.getSideHidden()).toBe(true);
  });

  it("nothing stored, or garbage stored, reads as open", async () => {
    expect((await reloadStore()).getSideHidden()).toBe(false);
    localStorage.setItem(SIDE_HIDDEN_KEY, "maybe");
    expect((await reloadStore()).getSideHidden()).toBe(false);
  });

  it("blocked storage costs the persistence, never the flag", async () => {
    const real = globalThis.localStorage;
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        throw new Error("SecurityError");
      },
    });
    try {
      const fresh = await reloadStore(); // seed must not throw
      expect(fresh.getSideHidden()).toBe(false);
      expect(() => fresh.setSideHidden(true)).not.toThrow();
      expect(fresh.getSideHidden()).toBe(true);
    } finally {
      Object.defineProperty(globalThis, "localStorage", {
        configurable: true,
        value: real,
        writable: true,
      });
    }
  });
});
