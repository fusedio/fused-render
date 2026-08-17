import { describe, expect, it } from "bun:test";
import { stripSessionParams } from "@platform/lib/session-params";

// WHAT A SESSION SIDECAR MAY NOT HOLD (LSN-12, D323). The hooks around this
// function are React and network, so this is the part a DOM-free test can pin —
// and it is the part that matters: the sidecar's whole job is to replay a query on
// a bare open, so a param that must not survive a refresh must not reach it.
describe("stripSessionParams", () => {
  it("drops `_side` on the way to and from the sidecar", () => {
    expect(stripSessionParams("_side=claude")).toBe("");
    expect(stripSessionParams("_side=off")).toBe("");
    expect(stripSessionParams("city=oslo&_side=git&limit=50")).toBe("city=oslo&limit=50");
    expect(stripSessionParams("_side=git&city=oslo")).toBe("city=oslo");
  });

  it("leaves everything else exactly as it was", () => {
    // Byte-for-byte, not re-encoded: a template's own params are literal (LSN-2's
    // "verbatim"), and round-tripping them through URLSearchParams would rewrite
    // spaces, commas and plus signs in a query nobody asked us to normalise.
    for (const q of ["", "_mode=code", "city=oslo&q=a+b%2Cc", "stretch=2,1471", "sel="]) {
      expect(stripSessionParams(q)).toBe(q);
    }
  });

  it("is not fooled by a param that merely starts with `_side`", () => {
    expect(stripSessionParams("_sidebar=1")).toBe("_sidebar=1");
    expect(stripSessionParams("x_side=1")).toBe("x_side=1");
  });

  it("drops a valueless `_side` too", () => {
    expect(stripSessionParams("_side")).toBe("");
    expect(stripSessionParams("a=1&_side&b=2")).toBe("a=1&b=2");
  });
});
