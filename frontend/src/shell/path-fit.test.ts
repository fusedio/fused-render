// The fit ladder, asserted the way the browser asks it: a `fits` that answers
// for ONE width, run again at the next width down. A character budget stands in
// for pixels — the real callback measures with `measureText` — because what is
// under test is WHICH rung answers at a given width, not how wide an `m` is.
import { describe, it, expect } from "bun:test";
import { ELLIPSIS, fitPathMiddle } from "./path-fit";

/** `fits` for a monospace world: at most `n` characters. */
const upTo = (n: number) => (s: string) => s.length <= n;

const DEEP = "~/Desktop/fused/fused-render/frontend"; // 37 chars, 5 segments
const ABS = "/Users/akshil/dev/fused-render"; // 30 chars, root + 4

describe("fitPathMiddle", () => {
  it("returns the path untouched when it fits", () => {
    expect(fitPathMiddle(DEEP, upTo(DEEP.length))).toBe(DEEP);
    expect(fitPathMiddle(DEEP, upTo(999))).toBe(DEEP);
    expect(fitPathMiddle("", upTo(0))).toBe("");
  });

  it("keeps the first two segments and the last when they fit", () => {
    expect(fitPathMiddle(DEEP, upTo(25))).toBe("~/Desktop/…/frontend");
  });

  it("drops to one head segment when two will not fit", () => {
    expect(fitPathMiddle(DEEP, upTo(15))).toBe("~/…/frontend");
  });

  it("drops the head entirely before it will touch the folder's name", () => {
    expect(fitPathMiddle(DEEP, upTo(11))).toBe("…/frontend");
  });

  it("cuts the name from the FRONT once nothing else is left to give", () => {
    // The end of a name is what tells two checkouts apart, so the end stays.
    expect(fitPathMiddle(DEEP, upTo(6))).toBe("…ntend");
    expect(fitPathMiddle(DEEP, upTo(3))).toBe("…nd");
  });

  it("never cuts below one character, however narrow the box claims to be", () => {
    expect(fitPathMiddle(DEEP, upTo(1))).toBe("…d");
    expect(fitPathMiddle(DEEP, () => false)).toBe("…d");
  });

  it("counts a leading `/` as part of the first segment", () => {
    expect(fitPathMiddle(ABS, upTo(25))).toBe("/Users/…/fused-render");
    expect(fitPathMiddle(ABS, upTo(18))).toBe("/…/fused-render");
  });

  it("skips the head rungs for a path with no middle to elide", () => {
    // `~/Desktop/fused-render` has three segments: a head of two would print
    // `~/Desktop/…/fused-render`, longer than what it replaced.
    const short = "~/Desktop/fused-render";
    expect(fitPathMiddle(short, upTo(15))).toBe("…/fused-render");
    expect(fitPathMiddle(short, upTo(10))).toBe("…ed-render");
    expect(fitPathMiddle("~/dev", upTo(4))).toBe("…dev");
    expect(fitPathMiddle("fused-render", upTo(8))).toBe("…-render");
  });

  it("reads a Windows path in the same alphabet as every other", () => {
    const win = "C:\\Users\\akshil\\dev\\fused-render";
    expect(fitPathMiddle(win, upTo(25))).toBe("C:/Users/…/fused-render");
    expect(fitPathMiddle(win, upTo(20))).toBe("C:/…/fused-render");
  });

  it("ignores a trailing separator rather than fitting around an empty tail", () => {
    expect(fitPathMiddle("~/Desktop/fused/fused-render/", upTo(25))).toBe(
      "~/Desktop/…/fused-render",
    );
    // The root has no tail to keep; there is nothing to shorten it TO, so it is
    // handed back whole rather than cut to a lone ellipsis.
    expect(fitPathMiddle("/", upTo(0))).toBe("/");
  });

  it("spells its cut with the one exported ellipsis", () => {
    expect(ELLIPSIS).toBe("…");
    expect(fitPathMiddle(DEEP, upTo(15)).startsWith("~/" + ELLIPSIS)).toBe(true);
  });
});
