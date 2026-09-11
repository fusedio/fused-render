import { afterEach, beforeEach, describe, expect, it } from "bun:test";

import { MIN_W } from "./side-width";

// bun's test runner has no DOM, so the store's one browser dependency is
// supplied here — a real Storage-shaped object, installed BEFORE the module is
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

// The width store PERSISTS since R1 feedback #29, so unlike its sibling
// `side-hidden-store.test.ts` there IS storage to clear between tests — and the
// seed happens at module load, so anything asserting about the seed has to
// import the module fresh rather than reuse the one this file already holds.
const {
  SIDE_WIDTH_KEY,
  clearSideWidth,
  getSideWidth,
  parseStoredSideWidth,
  setSideWidth,
  subscribeSideWidth,
} = await import("./side-store");

/** A fresh copy of the module, so its load-time seed runs against whatever is
 *  in storage now. Bun caches by specifier, so the query string is what makes
 *  each import a new module instance. */
let freshSeq = 0;
async function reloadStore() {
  return (await import(`./side-store?seed=${++freshSeq}`)) as typeof import("./side-store");
}

beforeEach(() => {
  localStorage.removeItem(SIDE_WIDTH_KEY);
  clearSideWidth();
});
afterEach(() => localStorage.removeItem(SIDE_WIDTH_KEY));

describe("parseStoredSideWidth", () => {
  it("no stored value is NO CHOICE, not a zero", () => {
    // `null` is a real state the consumers branch on (the column opens at the
    // container's share); a 0 would read as a chosen width.
    expect(parseStoredSideWidth(null)).toBeNull();
    expect(parseStoredSideWidth(undefined)).toBeNull();
    expect(parseStoredSideWidth("")).toBeNull();
  });

  it("a value that is not a number is no choice either", () => {
    expect(parseStoredSideWidth("wide")).toBeNull();
    expect(parseStoredSideWidth("NaN")).toBeNull();
    expect(parseStoredSideWidth("Infinity")).toBeNull();
  });

  it("a real width comes back, rounded", () => {
    expect(parseStoredSideWidth(String(MIN_W + 100))).toBe(MIN_W + 100);
    expect(parseStoredSideWidth(String(MIN_W + 100.6))).toBe(MIN_W + 101);
  });

  it("a width below the legibility floor is RAISED to it, never honoured", () => {
    // The floor is measured, not preferred (side-width.ts's MIN_W header), so a
    // hand-edited or corrupted value cannot open the column unusably narrow.
    expect(parseStoredSideWidth("40")).toBe(MIN_W);
    expect(parseStoredSideWidth("-999")).toBe(MIN_W);
  });
});

describe("side-store persistence (#29)", () => {
  it("a completed drag is written to storage", () => {
    setSideWidth(MIN_W + 120);
    expect(localStorage.getItem(SIDE_WIDTH_KEY)).toBe(String(MIN_W + 120));
  });

  it("a stored width is the store's answer before the first render", async () => {
    localStorage.setItem(SIDE_WIDTH_KEY, String(MIN_W + 220));
    const fresh = await reloadStore();
    // The two consumers read this in a `useState` initializer, so a seed that
    // arrived in an effect would paint the default share and then jump.
    expect(fresh.getSideWidth()).toBe(MIN_W + 220);
  });

  it("with nothing stored the store still starts at NO CHOICE", async () => {
    const fresh = await reloadStore();
    expect(fresh.getSideWidth()).toBeNull();
  });

  it("a stored value under the floor is clamped on the way in", async () => {
    localStorage.setItem(SIDE_WIDTH_KEY, "12");
    const fresh = await reloadStore();
    expect(fresh.getSideWidth()).toBe(MIN_W);
  });

  it("clearing the width removes the key rather than storing a null", () => {
    setSideWidth(MIN_W + 90);
    clearSideWidth();
    expect(localStorage.getItem(SIDE_WIDTH_KEY)).toBeNull();
    expect(getSideWidth()).toBeNull();
  });

  it("still notifies subscribers — the reopen drag depends on it", () => {
    let seen = 0;
    const off = subscribeSideWidth(() => (seen += 1));
    setSideWidth(MIN_W + 40);
    setSideWidth(MIN_W + 40); // same value: no event
    setSideWidth(MIN_W + 41);
    off();
    setSideWidth(MIN_W + 42);
    expect(seen).toBe(2);
  });

  it("blocked storage costs the persistence, never the drag", () => {
    // A private window, cleared site data, or a browser set to block storage
    // makes the accessor itself throw.
    const real = globalThis.localStorage;
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      get() {
        throw new Error("SecurityError");
      },
    });
    try {
      expect(() => setSideWidth(MIN_W + 60)).not.toThrow();
      expect(getSideWidth()).toBe(MIN_W + 60);
      expect(() => parseStoredSideWidth("500")).not.toThrow();
    } finally {
      Object.defineProperty(globalThis, "localStorage", {
        configurable: true,
        value: real,
        writable: true,
      });
    }
  });
});
