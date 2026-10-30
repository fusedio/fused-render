// The Where pill's words (where-label.ts): names that fit stay whole; longer
// ones lose their middle, never their start or end, within the pill's budget.
import { describe, expect, test } from "bun:test";

import { cutMiddle, WHERE_MAX_CHARS, whereParts } from "./where-label";

describe("cutMiddle", () => {
  test("keeps a string that fits", () => {
    expect(cutMiddle("my_canvas", 12)).toBe("my_canvas");
  });

  test("never splits an emoji", () => {
    const got = cutMiddle("🚀🚀🚀🚀🚀🚀🚀🚀🚀🚀", 5);
    expect(got).toBe("🚀🚀…🚀🚀");
    expect(got.includes("\uFFFD")).toBe(false);
  });

  test("never splits a grapheme cluster", () => {
    const family = "👨‍👩‍👧";
    const got = cutMiddle(family.repeat(6), 3);
    expect(got).toBe(`${family}…${family}`);
  });

  test("cuts the middle, keeping both ends", () => {
    expect(cutMiddle("abcdefghijkl", 7)).toBe("abc…jkl");
    expect(cutMiddle("abcdefghijkl", 8)).toBe("abcd…jkl");
  });
});

describe("whereParts", () => {
  const len = (p: { canvas: string; node: string | null }) => p.canvas.length + (p.node?.length ?? 0);

  test("short names stay whole", () => {
    expect(whereParts("my_canvas", "udf_one")).toEqual({ canvas: "my_canvas", node: "udf_one" });
  });

  test("canvas alone is cut to the budget", () => {
    const got = whereParts("a_really_long_canvas_name_for_testing", null);
    expect(got.node).toBeNull();
    expect(got.canvas.length).toBe(WHERE_MAX_CHARS);
    expect(got.canvas.startsWith("a_really")).toBe(true);
    expect(got.canvas.endsWith("testing")).toBe(true);
  });

  test("a long node never squeezes the canvas to an ellipsis", () => {
    const got = whereParts("akshil_canvas", "widget_1jhbhn_dsfkjsb_jkf_ksb_dfkhjbnsd_fhbsdxyz");
    expect(len(got)).toBe(WHERE_MAX_CHARS);
    expect(got.canvas.length).toBe(8);
    expect(got.node?.startsWith("widget")).toBe(true);
    expect(got.node?.endsWith("xyz")).toBe(true);
  });

  test("a short canvas keeps its whole name beside a long node", () => {
    const got = whereParts("qa", "widget_1jhbhn_dsfkjsb_jkf_ksb_dfkhjbnsd");
    expect(got.canvas).toBe("qa");
    expect(len(got)).toBe(WHERE_MAX_CHARS);
  });

  test("a small budget still leaves the node some of its own", () => {
    const got = whereParts("long_canvas_name", "node_x", 6);
    expect(len(got)).toBe(6);
    expect(got.node).not.toBe("…");
  });
});
