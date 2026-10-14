// openInTab: a host above → host.open and no window.open; no host, a host
// that throws, or one that refuses ("") → window.open with the caller's own
// features, none when none were passed.
import { afterEach, describe, expect, test } from "bun:test";

import { hasTabHost, openInTab } from "./open-in-tab";

type W = Record<string, unknown>;
const g = globalThis as unknown as W;
const saved = { window: g.window };

function fakeWindow(host?: unknown) {
  const opened: unknown[][] = [];
  const w: W = { open: (...a: unknown[]) => (opened.push(a), null) };
  w.top = w;
  if (host) w._fusedTabs = host;
  g.window = w;
  return opened;
}

afterEach(() => {
  g.window = saved.window;
});

describe("openInTab", () => {
  test("no host: window.open fallback", () => {
    const opened = fakeWindow();
    expect(hasTabHost()).toBe(false);
    expect(openInTab("/explorer/view/a", { features: "noopener" })).toBe(false);
    expect(openInTab("/explorer/view/b")).toBe(false);
    expect(opened).toEqual([["/explorer/view/a", "_blank", "noopener"], ["/explorer/view/b", "_blank"]]);
  });

  test("host: open with focusExisting default false", () => {
    const calls: unknown[][] = [];
    const opened = fakeWindow({ open: (...a: unknown[]) => (calls.push(a), "t1") });
    expect(hasTabHost()).toBe(true);
    expect(openInTab("/explorer/embed/a")).toBe(true);
    expect(calls).toEqual([["/explorer/embed/a", { focusExisting: false }]]);
    expect(opened).toEqual([]);
  });

  test("throwing host falls back", () => {
    const opened = fakeWindow({ open: () => { throw new Error("gone"); } });
    expect(openInTab("/x", { focusExisting: true, features: "noopener" })).toBe(false);
    expect(opened).toEqual([["/x", "_blank", "noopener"]]);
  });

  test("host refusing (\"\") falls back", () => {
    const opened = fakeWindow({ open: () => "" });
    expect(openInTab("/x")).toBe(false);
    expect(opened).toEqual([["/x", "_blank"]]);
  });
});
