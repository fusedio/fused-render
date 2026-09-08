// bun's test runtime has no localStorage, so stand one up — a real (tiny)
// store rather than a spy, since what matters is the round trip through a
// JSON-encoded string key, exactly where a serialization bug would hide.
import { beforeEach, expect, test } from "bun:test";
import { loadDismissed, saveDismissed } from "@shell/dismiss-store";

const store = new Map<string, string>();
(globalThis as { localStorage?: unknown }).localStorage = {
  getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
  setItem: (k: string, v: string) => void store.set(k, String(v)),
  removeItem: (k: string) => void store.delete(k),
  clear: () => store.clear(),
};

beforeEach(() => {
  store.clear();
});

test("nothing stored reads as an empty map", () => {
  expect(loadDismissed("k")).toEqual({});
});

test("a save round-trips through load, under its own key", () => {
  saveDismissed("fused-render:repo-updates-dismissed", { "/a": "sig-1" });
  expect(loadDismissed("fused-render:repo-updates-dismissed")).toEqual({ "/a": "sig-1" });
  // A different key sees nothing — two dismissal maps must never bleed into
  // each other.
  expect(loadDismissed("fused-render:attention-dismissed")).toEqual({});
});

test("malformed JSON is an empty map, not a throw", () => {
  store.set("k", "{oops");
  expect(loadDismissed("k")).toEqual({});
});

test("a stored value that is not an object is an empty map", () => {
  store.set("k", "null");
  expect(loadDismissed("k")).toEqual({});
  store.set("k", "[]");
  expect(loadDismissed("k")).toEqual({});
  store.set("k", '"just a string"');
  expect(loadDismissed("k")).toEqual({});
});

test("non-string values inside a valid object are dropped, entry by entry", () => {
  store.set("k", '{"/a":"sig-1","/b":42,"/c":null}');
  expect(loadDismissed("k")).toEqual({ "/a": "sig-1" });
});

test("unavailable storage reads as nothing dismissed, and never throws", () => {
  // Private mode, a full quota, a locked-down origin — a failed dismissal read
  // or write must degrade to "nothing dismissed" rather than break the panel.
  const real = (globalThis as { localStorage?: unknown }).localStorage;
  (globalThis as { localStorage?: unknown }).localStorage = {
    getItem: () => {
      throw new Error("denied");
    },
    setItem: () => {
      throw new Error("denied");
    },
  };
  try {
    expect(() => loadDismissed("k")).not.toThrow();
    expect(loadDismissed("k")).toEqual({});
    expect(() => saveDismissed("k", { "/a": "sig-1" })).not.toThrow();
  } finally {
    (globalThis as { localStorage?: unknown }).localStorage = real;
  }
});
