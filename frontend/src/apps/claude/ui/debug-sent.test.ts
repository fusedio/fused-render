// The "what was sent" door hangs only for a developer who set the flag.
import { afterEach, expect, test } from "bun:test";
import { DEBUG_KEY, debugSentEnabled } from "./debug-sent";

const g = globalThis as { localStorage?: unknown };
const had = g.localStorage;
afterEach(() => {
  g.localStorage = had;
});

const fakeStorage = (v: string | null) => ({ getItem: (k: string) => (k === DEBUG_KEY ? v : null) });

test("off by default — no storage, no key, or a key that is not '1'", () => {
  delete g.localStorage;
  expect(debugSentEnabled()).toBe(false);
  g.localStorage = fakeStorage(null);
  expect(debugSentEnabled()).toBe(false);
  g.localStorage = fakeStorage("true");
  expect(debugSentEnabled()).toBe(false);
});

test("on when the developer set it, and a storage that throws reads as off", () => {
  g.localStorage = fakeStorage("1");
  expect(debugSentEnabled()).toBe(true);
  g.localStorage = { getItem: () => { throw new Error("blocked"); } };
  expect(debugSentEnabled()).toBe(false);
});
