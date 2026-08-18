import { describe, expect, it } from "bun:test";
import { stripSessionParams } from "@platform/lib/session-params";

// WHAT NOTHING MAY PERSIST (LSN-12, D326). Its consumer is now the recents store
// (D329 removed the session sidecar, the rule's original caller), whose rows must
// hold what the file was and not what the chrome around it was doing — so a param
// that must not survive a refresh must not reach a recorded url.
describe("stripSessionParams", () => {
  it("drops `_side` from a recorded query", () => {
    expect(stripSessionParams("_side=claude")).toBe("");
    expect(stripSessionParams("_side=off")).toBe("");
    expect(stripSessionParams("city=oslo&_side=git&limit=50")).toBe("city=oslo&limit=50");
    expect(stripSessionParams("_side=git&city=oslo")).toBe("city=oslo");
  });

  it("leaves everything else exactly as it was", () => {
    // Byte-for-byte, not re-encoded: a template's own params are literal, and
    // round-tripping them through URLSearchParams would rewrite
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
