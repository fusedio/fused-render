// The ui-layout document's client-side sanitizer (ui-layout.ts), for the
// fields that round-trip numbers: a bad value 400s the whole PUT, so every
// field is clamped or dropped here first.
import { describe, expect, it } from "bun:test";
import { sanitizeUiLayout } from "./ui-layout";

describe("sanitizeUiLayout canvasRailPeek", () => {
  it("keeps a whole size, rounded", () => {
    expect(sanitizeUiLayout({ v: 1, canvasRailPeek: { width: 520.4, height: 719.6 } }).canvasRailPeek).toEqual({
      width: 520,
      height: 720,
    });
  });
  it("clamps to the server's bounds", () => {
    expect(sanitizeUiLayout({ v: 1, canvasRailPeek: { width: 10, height: 99999 } }).canvasRailPeek).toEqual({
      width: 120,
      height: 10000,
    });
  });
  it("drops a half size, a non-object, or junk numbers", () => {
    expect(sanitizeUiLayout({ v: 1, canvasRailPeek: { width: 520 } }).canvasRailPeek).toBeUndefined();
    expect(sanitizeUiLayout({ v: 1, canvasRailPeek: [520, 720] }).canvasRailPeek).toBeUndefined();
    expect(sanitizeUiLayout({ v: 1, canvasRailPeek: { width: "520", height: 720 } }).canvasRailPeek).toBeUndefined();
    expect(sanitizeUiLayout({ v: 1, canvasRailPeek: { width: NaN, height: 720 } }).canvasRailPeek).toBeUndefined();
  });
});

describe("sanitizeUiLayout canvasRailFav", () => {
  it("keeps the favourite's card size in the same shape, clamped", () => {
    expect(sanitizeUiLayout({ v: 1, canvasRailFav: { width: 400.4, height: 99999 } }).canvasRailFav).toEqual({
      width: 400,
      height: 10000,
    });
    expect(sanitizeUiLayout({ v: 1, canvasRailFav: { width: 400 } }).canvasRailFav).toBeUndefined();
  });
});

describe("sanitizeUiLayout chatTextScale", () => {
  it("keeps a step, snaps an off-step number and clamps to 0.8–2", () => {
    expect(sanitizeUiLayout({ v: 1, chatTextScale: 1.25 }).chatTextScale).toBe(1.25);
    expect(sanitizeUiLayout({ v: 1, chatTextScale: 1.3 }).chatTextScale).toBe(1.25);
    expect(sanitizeUiLayout({ v: 1, chatTextScale: 0.1 }).chatTextScale).toBe(0.8);
    expect(sanitizeUiLayout({ v: 1, chatTextScale: 40 }).chatTextScale).toBe(2);
  });
  it("drops a non-number", () => {
    expect(sanitizeUiLayout({ v: 1, chatTextScale: "1.25" }).chatTextScale).toBeUndefined();
    expect(sanitizeUiLayout({ v: 1, chatTextScale: NaN }).chatTextScale).toBeUndefined();
    expect(sanitizeUiLayout({ v: 1, chatTextScale: null }).chatTextScale).toBeUndefined();
  });
});
