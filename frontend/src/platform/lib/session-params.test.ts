import { describe, expect, it } from "bun:test";
import { restoredSearch, stripSessionParams } from "@platform/lib/session-params";

// WHAT A SESSION SIDECAR MAY NOT HOLD (LSN-12, D326). The hooks around this
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

// WHAT A RESTORE ACTUALLY WRITES. Two rules have to hold at once, and getting one
// without the other is a bug each way round:
//
//   the sidecar's params are replayed, minus the omitted ones (LSN-4 + LSN-12);
//   the LIVE url's omitted params SURVIVE the write, because they are not the
//   sidecar's business and the restore replaces the whole query.
//
// Without the second, a refresh of `?_side=off` on a file that HAS a session would
// silently reopen the sidebar — the restore would replace the query with the
// sidecar's and drop the user's close on the way.
describe("restoredSearch", () => {
  it("replays the sidecar's params without the omitted ones", () => {
    expect(restoredSearch("city=oslo&_side=git", "")).toBe("city=oslo");
    expect(restoredSearch("city=oslo", "")).toBe("city=oslo");
  });

  it("carries the LIVE `_side` through the restore", () => {
    expect(restoredSearch("city=oslo", "_side=off")).toBe("city=oslo&_side=off");
    // The stored one never competes with it.
    expect(restoredSearch("city=oslo&_side=claude", "_side=off")).toBe("city=oslo&_side=off");
  });

  it("is EMPTY when there is nothing to replay, whatever the live url holds", () => {
    // Empty means "do not write" — so a `?_side=off` url with no session (or a
    // `_side`-only sidecar) keeps the query it already has, untouched.
    expect(restoredSearch("", "_side=off")).toBe("");
    expect(restoredSearch("_side=claude", "_side=off")).toBe("");
    expect(restoredSearch("", "")).toBe("");
  });

  it("keeps both sides byte-identical", () => {
    expect(restoredSearch("stretch=2,1471", "_side=git")).toBe("stretch=2,1471&_side=git");
  });
});
