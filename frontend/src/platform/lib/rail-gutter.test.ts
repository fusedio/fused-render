import { afterAll, beforeAll, describe, expect, it } from "bun:test";
import { railCollisionPadding, railGutter } from "./rail-gutter";

// A document with one root element and a computed-style reader that answers
// off its inline style — all `railGutter` reads. Installed and restored by
// this file (bun-test-location-leak: a suite never deletes a global it did
// not make).
const g = globalThis as { document?: unknown; getComputedStyle?: unknown };
const had = { document: g.document, getComputedStyle: g.getComputedStyle };
const vars = new Map<string, string>();
const root = {
  style: {
    setProperty: (k: string, v: string) => void vars.set(k, v),
    removeProperty: (k: string) => void vars.delete(k),
  },
};

describe("railGutter (the Claude rail's width, said on the document)", () => {
  beforeAll(() => {
    g.document = { documentElement: root };
    g.getComputedStyle = () => ({ getPropertyValue: (k: string) => vars.get(k) ?? "" });
  });
  afterAll(() => {
    g.document = had.document;
    g.getComputedStyle = had.getComputedStyle;
  });
  it("no rail: 0, and the positioner's padding is its plain 5px", () => {
    vars.clear();
    expect(railGutter()).toBe(0);
    expect(railCollisionPadding()).toEqual({ top: 5, right: 5, bottom: 5, left: 5 });
  });
  it("a rail standing: its width, added to the right-hand padding only", () => {
    root.style.setProperty("--claude-rail-w", "48px");
    expect(railGutter()).toBe(48);
    expect(railCollisionPadding()).toEqual({ top: 5, right: 53, bottom: 5, left: 5 });
    root.style.removeProperty("--claude-rail-w");
    expect(railGutter()).toBe(0);
  });
});
